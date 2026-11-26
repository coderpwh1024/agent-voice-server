"""Bounded async access to Mem0's synchronous SDK and PostgreSQL resources."""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from time import monotonic
from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from app.core.database import get_postgres_connection_string, validate_postgres_config
from app.core.settings import settings
from app.repositories.mem0_history import PostgresHistory
from app.schemas.memory import (
    MemoryDeleteResult,
    MemoryHistory,
    MemoryHistoryEntry,
    MemoryList,
    MemoryRecord,
)
from app.schemas.memory_commands import MemoryMutation, MemoryMutationResult, MemorySnapshot
from app.services.memory.mem0_rerank import Mem0Reranker
from app.services.memory.mem0_sdk import build_memory, close_memory

logger = logging.getLogger(__name__)


AGENT_ID = "mem0-chatbot"


class MemoryNotFoundError(LookupError):
    pass


class MemoryBusyError(RuntimeError):
    pass


class MemoryConflictError(RuntimeError):
    pass


class Mem0Service:
    def __init__(
        self,
        memory: Any,
        executor: ThreadPoolExecutor,
        *,
        reranker: Mem0Reranker | None = None,
        max_pending: int = 8,
    ) -> None:
        self.memory = memory
        self.executor = executor
        self.reranker = reranker
        self.pending: set[asyncio.Future[Any]] = set()
        self.max_pending = max_pending

    def _run_scoped(
        self, function: Callable[..., Any], user_id: str, kwargs: dict[str, Any]
    ) -> Any:
        if isinstance(self.memory.db, PostgresHistory):
            with self.memory.db.user_lock(user_id, AGENT_ID):
                return function(**kwargs)
        return function(**kwargs)

    async def _call(
        self,
        operation: str,
        function: Callable[..., Any],
        *,
        scope_user: str,
        **kwargs: Any,
    ) -> Any:
        if len(self.pending) >= self.max_pending:
            raise MemoryBusyError("Mem0 is busy; retry on the next turn")
        started = monotonic()
        future = asyncio.get_running_loop().run_in_executor(
            self.executor, partial(self._run_scoped, function, scope_user, kwargs)
        )
        self.pending.add(future)
        future.add_done_callback(self.pending.discard)
        try:
            async with asyncio.timeout(settings.MEM0_TIMEOUT_SECONDS):
                result = await asyncio.shield(future)
            self.pending.discard(future)
            logger.info(
                "Mem0 operation completed operation=%s duration_ms=%d",
                operation,
                int((monotonic() - started) * 1000),
            )
            return result
        except (Exception, asyncio.CancelledError):
            # Cancellation cannot kill a running SDK worker. Observe its result and
            # keep capacity reserved until it finishes, including during shutdown.
            future.add_done_callback(
                lambda done: done.exception() if not done.cancelled() else None
            )
            raise

    def _search_candidates(self, query: str, user_id: str, top_k: int) -> list[dict[str, Any]]:
        candidate_count = max(top_k, settings.MEM0_RERANK_CANDIDATES) if self.reranker else top_k
        result = self.memory.search(
            query=query,
            filters={"user_id": user_id, "agent_id": AGENT_ID},
            top_k=candidate_count,
            threshold=settings.MEM0_THRESHOLD,
        )
        candidates = result.get("results", [])
        if not self.reranker or len(candidates) < 2:
            return candidates[:top_k]
        try:
            return self.reranker.rerank(query, candidates, top_k)
        except Exception as exc:
            logger.warning("Mem0 rerank failed error_type=%s", type(exc).__name__)
            return candidates[:top_k]

    async def search(self, query: str, user_id: str) -> list[str]:
        candidates = await self._call(
            "search",
            self._search_candidates,
            scope_user=user_id,
            query=query,
            top_k=settings.MEM0_TOP_K,
            user_id=user_id,
        )
        memories: list[str] = []
        remaining = settings.MEM0_MAX_CONTEXT_CHARS
        for item in candidates:
            text = item.get("memory")
            if isinstance(text, str) and text.strip() and remaining > 0:
                memories.append(text[:remaining])
                remaining -= len(memories[-1])
        logger.info("Mem0 search returned count=%d", len(memories))
        return memories

    def _remember(
        self,
        messages: list[dict[str, str]],
        user_id: str,
        thread_id: str,
        context: list[dict[str, str]] | None,
    ) -> dict[str, Any]:
        if not isinstance(self.memory.db, PostgresHistory):
            raise TypeError("Mem0 extraction requires PostgreSQL history")
        with self.memory.db.extraction_scope(thread_id, context):
            return self.memory.add(
                messages=messages,
                user_id=user_id,
                agent_id=AGENT_ID,
                metadata={"source_thread_id": thread_id},
            )

    async def remember(
        self,
        messages: list[dict[str, str]],
        user_id: str,
        thread_id: str,
        *,
        context: list[dict[str, str]] | None = None,
    ) -> None:
        result = await self._call(
            "add",
            self._remember,
            scope_user=user_id,
            messages=messages,
            user_id=user_id,
            thread_id=thread_id,
            context=context,
        )
        logger.info("Mem0 write returned count=%d", len(result.get("results", [])))

    def _owned_memory(self, memory_id: str, user_id: str) -> dict[str, Any]:
        record = self.memory.get(memory_id=memory_id)
        if not record or record.get("user_id") != user_id or record.get("agent_id") != AGENT_ID:
            raise MemoryNotFoundError("Memory not found")
        return record

    @staticmethod
    def _record(data: dict[str, Any]) -> MemoryRecord:
        if data.get("metadata") is None:
            data = {**data, "metadata": {}}
        return MemoryRecord.model_validate(data)

    def _list(self, user_id: str, limit: int, cursor: UUID | None) -> MemoryList:
        rows = self.memory.db.list_memories(user_id, AGENT_ID, limit + 1, cursor)
        records = [
            MemoryRecord(
                id=row["id"],
                memory=row["data"],
                user_id=row["user_id"],
                agent_id=row["agent_id"],
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                metadata={
                    key: value
                    for key, value in row.items()
                    if key
                    not in {
                        "id",
                        "data",
                        "user_id",
                        "agent_id",
                        "created_at",
                        "updated_at",
                        "hash",
                        "text_lemmatized",
                    }
                },
            )
            for row in rows[:limit]
        ]
        return MemoryList(
            memories=records,
            next_cursor=records[-1].id if len(rows) > limit else None,
        )

    async def list_memories(
        self, user_id: str, limit: int = 50, cursor: UUID | None = None
    ) -> MemoryList:
        return await self._call(
            "list", self._list, scope_user=user_id, user_id=user_id, limit=limit, cursor=cursor
        )

    def _get(self, memory_id: str, user_id: str) -> MemoryRecord:
        return self._record(self._owned_memory(memory_id, user_id))

    async def get_memory(self, memory_id: str, user_id: str) -> MemoryRecord:
        return await self._call(
            "get", self._get, scope_user=user_id, memory_id=memory_id, user_id=user_id
        )

    def _update(self, memory_id: str, user_id: str, text: str) -> MemoryRecord:
        record = self._owned_memory(memory_id, user_id)
        if record["memory"] == text:
            return self._record(record)
        self.memory.db.bind_history_owner(memory_id, user_id, AGENT_ID)
        self.memory.update(memory_id=memory_id, text=text)
        self.memory.db.clear_context(user_id, AGENT_ID)
        return self._get(memory_id, user_id)

    async def update_memory(self, memory_id: str, user_id: str, text: str) -> MemoryRecord:
        return await self._call(
            "update",
            self._update,
            scope_user=user_id,
            memory_id=memory_id,
            user_id=user_id,
            text=text,
        )

    def _delete(self, memory_id: str, user_id: str) -> MemoryDeleteResult:
        self._owned_memory(memory_id, user_id)
        self.memory.db.bind_history_owner(memory_id, user_id, AGENT_ID)
        with self.memory.db.history_scope(memory_id, user_id, AGENT_ID):
            self.memory.delete(memory_id=memory_id)
        self.memory.db.clear_context(user_id, AGENT_ID)
        return MemoryDeleteResult(deleted_count=1)

    async def delete_memory(self, memory_id: str, user_id: str) -> MemoryDeleteResult:
        return await self._call(
            "delete", self._delete, scope_user=user_id, memory_id=memory_id, user_id=user_id
        )

    def _delete_all(self, user_id: str) -> MemoryDeleteResult:
        self.memory.db.clear_context(user_id, AGENT_ID)
        deleted = 0
        while rows := self.memory.db.list_memories(user_id, AGENT_ID, 100):
            for row in rows:
                self._owned_memory(row["id"], user_id)
                self.memory.db.bind_history_owner(row["id"], user_id, AGENT_ID)
                with self.memory.db.history_scope(row["id"], user_id, AGENT_ID):
                    self.memory.delete(memory_id=row["id"])
                deleted += 1
        return MemoryDeleteResult(deleted_count=deleted)

    async def delete_all_memories(self, user_id: str) -> MemoryDeleteResult:
        return await self._call("delete_all", self._delete_all, scope_user=user_id, user_id=user_id)

    def _history(
        self, memory_id: str, user_id: str, limit: int, cursor: int | None
    ) -> MemoryHistory:
        record = self.memory.get(memory_id=memory_id)
        owner = record if record else self.memory.db.history_owner(memory_id)
        if not owner or owner.get("user_id") != user_id or owner.get("agent_id") != AGENT_ID:
            raise MemoryNotFoundError("Memory not found")
        rows = self.memory.db.history_page(memory_id, limit + 1, cursor)
        entries = [MemoryHistoryEntry.model_validate(row) for row in rows[:limit]]
        return MemoryHistory(
            history=entries,
            next_cursor=entries[-1].id if len(rows) > limit else None,
        )

    async def memory_history(
        self, memory_id: str, user_id: str, limit: int = 50, cursor: int | None = None
    ) -> MemoryHistory:
        return await self._call(
            "history",
            self._history,
            scope_user=user_id,
            memory_id=memory_id,
            user_id=user_id,
            limit=limit,
            cursor=cursor,
        )

    def _search_records(self, query: str, user_id: str) -> MemoryList:
        candidates = self._search_candidates(query, user_id, 5)
        records = [self._get(item["id"], user_id) for item in candidates]
        return MemoryList(memories=records)

    async def search_memory_records(self, query: str, user_id: str) -> MemoryList:
        return await self._call(
            "search_records",
            self._search_records,
            scope_user=user_id,
            query=query,
            user_id=user_id,
        )

    def _snapshot(self, user_id: str) -> MemorySnapshot:
        digest = hashlib.sha256()
        cursor = None
        count = 0
        while rows := self.memory.db.list_memories(user_id, AGENT_ID, 100, cursor):
            for row in rows:
                digest.update(
                    json.dumps(
                        [row["id"], row["data"], row.get("updated_at")], ensure_ascii=False
                    ).encode()
                )
                digest.update(b"\n")
            count += len(rows)
            cursor = UUID(rows[-1]["id"])
        return MemorySnapshot(count=count, revision=digest.hexdigest())

    async def memory_snapshot(self, user_id: str) -> MemorySnapshot:
        return await self._call("snapshot", self._snapshot, scope_user=user_id, user_id=user_id)

    def _apply_mutation(self, user_id: str, mutation: MemoryMutation) -> MemoryMutationResult:
        if mutation.action == "delete_many":
            for target in mutation.targets:
                record = self._owned_memory(str(target.id), user_id)
                if (
                    record["memory"] != target.memory
                    or record.get("updated_at") != target.updated_at
                ):
                    raise MemoryConflictError("A selected memory changed after it was selected")
            for target in mutation.targets:
                self._delete(str(target.id), user_id)
            return MemoryMutationResult(status="deleted", deleted_count=len(mutation.targets))
        if mutation.action == "delete_all":
            snapshot = self._snapshot(user_id)
            if snapshot.count == 0:
                self.memory.db.clear_context(user_id, AGENT_ID)
                return MemoryMutationResult(status="cleared")
            if snapshot.revision != mutation.revision:
                raise MemoryConflictError("Memories changed after confirmation was requested")
            result = self._delete_all(user_id)
            return MemoryMutationResult(status="cleared", deleted_count=result.deleted_count)
        target = mutation.target
        if target is None:
            raise ValueError("A memory target is required")
        memory_id = str(target.id)
        record = self._owned_memory(memory_id, user_id)
        if mutation.action == "update" and record["memory"] == mutation.text:
            return MemoryMutationResult(status="unchanged")
        if record["memory"] != target.memory or record.get("updated_at") != target.updated_at:
            raise MemoryConflictError("The target memory changed after it was selected")
        if mutation.action == "delete":
            self._delete(memory_id, user_id)
            return MemoryMutationResult(status="deleted", deleted_count=1)
        if mutation.text is None:
            raise ValueError("Replacement memory text is required")
        self._update(memory_id, user_id, mutation.text)
        return MemoryMutationResult(status="updated")

    async def apply_memory_mutation(
        self, user_id: str, mutation: MemoryMutation
    ) -> MemoryMutationResult:
        """Check the spoken preview and mutate under the same user lock."""
        return await self._call(
            "apply_mutation",
            self._apply_mutation,
            scope_user=user_id,
            user_id=user_id,
            mutation=mutation,
        )


_active_service: Mem0Service | None = None


def get_mem0_service() -> Mem0Service | None:
    if settings.MEM0_ENABLED and _active_service is None:
        raise RuntimeError("Mem0 has not been initialized by the service lifecycle")
    return _active_service if settings.MEM0_ENABLED else None


@asynccontextmanager
async def mem0_lifespan() -> AsyncIterator[None]:
    global _active_service
    if not settings.MEM0_ENABLED:
        yield
        return
    validate_postgres_config()
    settings.require_mem0_configuration()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mem0")
    pool = ConnectionPool(
        get_postgres_connection_string(),
        min_size=1,
        max_size=2,
        open=False,
        timeout=settings.MEM0_TIMEOUT_SECONDS,
        kwargs={
            "connect_timeout": int(settings.MEM0_TIMEOUT_SECONDS),
            "options": f"-c statement_timeout={int(settings.MEM0_TIMEOUT_SECONDS * 1000)}",
        },
    )
    memory = None
    reranker = None
    try:
        if settings.MEM0_RERANK_MODEL and settings.MEM0_RERANK_URL:
            key = settings.MEM0_API_KEY or settings.DASHSCOPE_API_KEY
            if key is None:
                raise ValueError("Mem0 reranking requires an API key")
            reranker = Mem0Reranker(
                settings.MEM0_RERANK_MODEL,
                settings.MEM0_RERANK_URL,
                key.get_secret_value(),
                settings.MEM0_RERANK_TIMEOUT_SECONDS,
            )
        await asyncio.to_thread(pool.open, wait=True, timeout=settings.MEM0_TIMEOUT_SECONDS)
        initialization = asyncio.get_running_loop().run_in_executor(
            executor, build_memory, settings, pool
        )
        try:
            memory = await asyncio.shield(initialization)
        except asyncio.CancelledError:
            memory = await initialization
            raise
        _active_service = Mem0Service(memory, executor, reranker=reranker)
        logger.info(
            "Mem0 initialized backend=pgvector collection=%s", settings.MEM0_COLLECTION_NAME
        )
        yield
    finally:
        service = _active_service
        _active_service = None
        if service and service.pending:
            await asyncio.gather(*service.pending, return_exceptions=True)
        try:
            if memory is not None:
                await asyncio.to_thread(close_memory, memory)
        finally:
            try:
                if reranker is not None:
                    await asyncio.to_thread(reranker.close)
            finally:
                try:
                    await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
                finally:
                    await asyncio.to_thread(pool.close)
        logger.info("Mem0 resources closed")
