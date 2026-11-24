"""PostgreSQL task ownership, reservations and crash-recoverable queue leases."""

import asyncio
import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Any, LiteralString, cast

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.sql import SQL, Composable, Identifier
from psycopg_pool import AsyncConnectionPool

from app.models import SQL_DIRECTORY
from app.models.image import (
    ImageBusyError,
    ImageClaim,
    ImageConflictError,
    ImageEditLimitError,
    ImageLoopProcessingError,
    ImageProgress,
)
from app.schemas.image import (
    ImageCreate,
    ImageListQuery,
    ImageLoop,
    ImageLoopExit,
    ImageLoopHistory,
    ImageLoopHistoryPage,
    ImageLoopHistoryQuery,
    ImageLoopState,
    ImageModel,
    ImageStatus,
    ImageTask,
    ImageTaskPage,
)

_COLUMNS = SQL("""
    id, title, prompt, model, status, source_image_id, source_file_ids, size,
    image_url, error_code, create_time, update_time
""")
_LOOP_COLUMNS = SQL("""
    image_id AS id, title, prompt, model, status, source_image_id, source_file_ids,
    size, image_url, error_code, create_time, update_time,
    root_image_id AS loop_root_image_id, version AS loop_version
""")
_MAIN_TABLE = Identifier("image_generation_tasks")
_LOOP_TABLE = Identifier("image_edit_loop_versions")
_ACTIVE = SQL("status IN ('QUEUED', 'SUBMITTING', 'PENDING', 'RUNNING', 'STORING')")
_SUBMITTED = SQL("status IN ('SUBMITTING', 'PENDING', 'RUNNING')")
_HISTORY_ROOTS = SQL("""
    FROM image_generation_tasks AS root
    JOIN (
        SELECT root_image_id, user_id, create_time AS loop_create_time
        FROM image_edit_loop_versions WHERE version = 1
    ) AS first_edit ON first_edit.root_image_id = root.id AND first_edit.user_id = root.user_id
    WHERE root.user_id = %s AND root.is_delete = 0
""")


class ImageRepository:
    def __init__(self, conninfo: str) -> None:
        self.pool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
            conninfo,
            min_size=1,
            max_size=4,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

    async def open(self) -> None:
        await self.pool.open(wait=True)
        paths = (
            SQL_DIRECTORY / "create_image_generation_tasks.sql",
            SQL_DIRECTORY / "create_image_edit_loop_versions.sql",
        )
        ddl = await asyncio.gather(
            *(asyncio.to_thread(path.read_text, encoding="utf-8") for path in paths)
        )
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '10s'")
            await conn.execute("SELECT pg_advisory_xact_lock(824567123)")
            for statement in ddl:
                await conn.execute(SQL(cast(LiteralString, statement)))

    async def close(self) -> None:
        await self.pool.close()

    async def find_replay(self, user_id: int, key: str, request_hash: str) -> ImageTask | None:
        async with self.pool.connection() as conn:
            return await self._find_replay(conn, user_id, key, request_hash)

    @staticmethod
    async def _find_replay(
        conn: AsyncConnection[dict[str, Any]], user_id: int, key: str, request_hash: str
    ) -> ImageTask | None:
        loop_row = await (
            await conn.execute(
                SQL("""
                    SELECT {}, request_hash, is_delete FROM image_edit_loop_versions
                    WHERE user_id = %s AND idempotency_key = %s
                """).format(_LOOP_COLUMNS),
                (user_id, key),
            )
        ).fetchone()
        if loop_row is not None and loop_row["is_delete"]:
            raise ImageConflictError("这条图像请求对应的版本已删除")
        row = (
            loop_row
            or await (
                await conn.execute(
                    SQL("""
                    SELECT {}, request_hash FROM image_generation_tasks
                    WHERE user_id = %s AND idempotency_key = %s AND is_delete = 0
                """).format(_COLUMNS),
                    (user_id, key),
                )
            ).fetchone()
        )
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ImageConflictError("幂等键已用于其他图像请求")
        return ImageTask.model_validate(row)

    @staticmethod
    async def _lock_user(conn: AsyncConnection[dict[str, Any]], user_id: int) -> None:
        lock_id = int.from_bytes(
            hashlib.sha256(f"image-user:{user_id}".encode()).digest()[:8], "big", signed=True
        )
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))

    async def enqueue(
        self,
        user_id: int,
        key: str,
        request_hash: str,
        body: ImageCreate,
        *,
        model: ImageModel,
        file_urls: list[str],
        source_image_id: int | None,
        loop: bool,
        loop_max_edits: int,
        max_active_per_user: int,
    ) -> tuple[ImageTask, bool]:
        async with self.pool.connection() as conn, conn.transaction():
            await self._lock_user(conn, user_id)
            if existing := await self._find_replay(conn, user_id, key, request_hash):
                return existing, False
            if loop:
                deleted = await (
                    await conn.execute(
                        """
                        SELECT id FROM image_generation_tasks
                        WHERE user_id = %s AND idempotency_key = %s AND is_delete = 1 LIMIT 1
                        """,
                        (user_id, key),
                    )
                ).fetchone()
                if deleted is not None:
                    raise ImageConflictError("这条语音请求对应的图像已删除")
            source = None
            root_image_id = None
            if source_image_id is not None:
                source = await self._source(conn, source_image_id, user_id)
                if source is None:
                    raise LookupError("原图不存在")
                if source["image_url"] is None:
                    raise ImageConflictError("原图尚未成功生成")
                root_image_id = source["root_image_id"]
            version = None
            latest = None
            if source_image_id is not None and root_image_id is None:
                latest = await self._latest_loop(conn, source_image_id, user_id)
                if loop or latest is not None:
                    root_image_id = source_image_id
            if root_image_id is not None:
                if root_image_id != source_image_id:
                    await self._require_root(conn, root_image_id, user_id)
                    latest = await self._latest_loop(conn, root_image_id, user_id)
                if latest is not None and latest["loop_exit"]:
                    raise ImageConflictError("这次图像创作已结束")
                version = 1 if latest is None else latest["version"] + 1
                if version > loop_max_edits:
                    raise ImageEditLimitError(loop_max_edits)
                if latest is not None and ImageStatus(latest["status"]).active:
                    raise ImageLoopProcessingError("这个图像版本仍在处理中，请稍后再编辑")
            active = await (
                await conn.execute(
                    SQL("""
                        SELECT COUNT(*) AS total FROM image_work_queue
                        WHERE user_id = %s AND is_delete = 0 AND {}
                    """).format(_ACTIVE),
                    (user_id,),
                )
            ).fetchone()
            if active and active["total"] >= max_active_per_user:
                raise ImageBusyError("当前图像任务较多，请等待已有任务完成")
            source_urls = [source["image_url"], *file_urls] if source else file_urls
            title = body.title or body.prompt.splitlines()[0][:80]
            values = (
                user_id,
                title,
                body.prompt,
                model,
                body.size,
                source_image_id,
                body.source_file_ids,
                source_urls,
            )
            audit = (key, request_hash, user_id, user_id)
            if root_image_id is not None:
                row = await (
                    await conn.execute(
                        SQL("""
                            INSERT INTO image_edit_loop_versions (
                                user_id, title, prompt, model, size, source_image_id,
                                source_file_ids, source_urls, root_image_id, version,
                                idempotency_key, request_hash, create_by, update_by
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING {}
                        """).format(_LOOP_COLUMNS),
                        (*values, root_image_id, version, *audit),
                    )
                ).fetchone()
                return ImageTask.model_validate(row), True
            row = await (
                await conn.execute(
                    SQL("""
                        INSERT INTO image_generation_tasks (
                            user_id, title, prompt, model, size, source_image_id, source_file_ids,
                            source_urls, edit_depth, idempotency_key, request_hash, create_by, update_by
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING {}
                    """).format(_COLUMNS),
                    (*values, source["edit_depth"] + 1 if source else 0, *audit),
                )
            ).fetchone()
            return ImageTask.model_validate(row), True

    @staticmethod
    async def _source(
        conn: AsyncConnection[dict[str, Any]], image_id: int, user_id: int
    ) -> dict[str, Any] | None:
        main = await (
            await conn.execute(
                """
                SELECT image_url, edit_depth, NULL AS root_image_id FROM image_generation_tasks
                WHERE id = %s AND user_id = %s AND is_delete = 0 FOR UPDATE
                """,
                (image_id, user_id),
            )
        ).fetchone()
        return (
            main
            or await (
                await conn.execute(
                    """
                SELECT image_url, root_image_id FROM image_edit_loop_versions
                WHERE image_id = %s AND user_id = %s AND is_delete = 0 FOR UPDATE
                """,
                    (image_id, user_id),
                )
            ).fetchone()
        )

    @staticmethod
    async def _require_root(
        conn: AsyncConnection[dict[str, Any]], root_image_id: int, user_id: int
    ) -> None:
        row = await (
            await conn.execute(
                """
                SELECT id FROM image_generation_tasks
                WHERE id = %s AND user_id = %s AND is_delete = 0
                """,
                (root_image_id, user_id),
            )
        ).fetchone()
        if row is None:
            raise LookupError("根图不存在")

    @staticmethod
    async def _latest_loop(
        conn: AsyncConnection[dict[str, Any]], root_image_id: int, user_id: int
    ) -> dict[str, Any] | None:
        return await (
            await conn.execute(
                """
                SELECT version, status, loop_exit, accepted_image_id FROM image_edit_loop_versions
                WHERE root_image_id = %s AND user_id = %s ORDER BY version DESC LIMIT 1
                """,
                (root_image_id, user_id),
            )
        ).fetchone()

    async def get(self, image_id: int, user_id: int) -> ImageTask | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    SQL("""
                        SELECT {} FROM image_generation_tasks
                        WHERE id = %s AND user_id = %s AND is_delete = 0
                    """).format(_COLUMNS),
                    (image_id, user_id),
                )
            ).fetchone()
            if row is None:
                row = await (
                    await conn.execute(
                        SQL("""
                            SELECT {} FROM image_edit_loop_versions
                            WHERE image_id = %s AND user_id = %s AND is_delete = 0
                        """).format(_LOOP_COLUMNS),
                        (image_id, user_id),
                    )
                ).fetchone()
        return ImageTask.model_validate(row) if row else None

    async def get_loop(self, image_id: int, user_id: int, max_edits: int) -> ImageLoop:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            root_image_id = await self._loop_root(conn, image_id, user_id)
            return await self._loop_summary(conn, root_image_id, user_id, max_edits)

    async def list_loop_history(
        self, user_id: int, query: ImageLoopHistoryQuery, max_edits: int
    ) -> ImageLoopHistoryPage:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            count = await (
                await conn.execute(
                    SQL("SELECT COUNT(*) AS total {}").format(_HISTORY_ROOTS), (user_id,)
                )
            ).fetchone()
            roots = await (
                await conn.execute(
                    SQL("""
                        SELECT {}, loop_create_time {}
                        ORDER BY loop_create_time DESC, first_edit.root_image_id DESC
                        LIMIT %s OFFSET %s
                    """).format(_COLUMNS, _HISTORY_ROOTS),
                    (user_id, query.page_size, (query.page - 1) * query.page_size),
                )
            ).fetchall()
            items = await self._loop_history(conn, roots, user_id, max_edits)
        total = count["total"] if count else 0
        return ImageLoopHistoryPage(
            items=items,
            total=total,
            page=query.page,
            page_size=query.page_size,
            total_pages=(total + query.page_size - 1) // query.page_size,
        )

    async def get_loop_history(
        self, root_image_id: int, user_id: int, max_edits: int
    ) -> ImageLoopHistory:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            root = await (
                await conn.execute(
                    SQL("SELECT {}, loop_create_time {} AND root.id = %s").format(
                        _COLUMNS, _HISTORY_ROOTS
                    ),
                    (user_id, root_image_id),
                )
            ).fetchone()
            if root is None:
                raise LookupError("图像编辑历史不存在")
            return (await self._loop_history(conn, [root], user_id, max_edits))[0]

    async def _loop_history(
        self,
        conn: AsyncConnection[dict[str, Any]],
        roots: list[dict[str, Any]],
        user_id: int,
        max_edits: int,
    ) -> list[ImageLoopHistory]:
        if not roots:
            return []
        root_ids = [root["id"] for root in roots]
        latest_rows = await (
            await conn.execute(
                """
                SELECT DISTINCT ON (root_image_id)
                    root_image_id, version, status, loop_exit, accepted_image_id,
                    MAX(update_time) OVER (PARTITION BY root_image_id) AS loop_update_time
                FROM image_edit_loop_versions
                WHERE user_id = %s AND root_image_id = ANY(%s)
                ORDER BY root_image_id, version DESC
                """,
                (user_id, root_ids),
            )
        ).fetchall()
        latest_by_root = {row["root_image_id"]: row for row in latest_rows}
        versions_by_root = await self._loop_versions(conn, root_ids, user_id)
        items = []
        for row in roots:
            root = ImageTask.model_validate(row)
            latest = latest_by_root[root.id]
            versions = versions_by_root[root.id]
            state = self._loop_state(root.id, latest, max_edits)
            preview = next(
                (image for image in (root, *versions) if image.id == state.accepted_image_id),
                None,
            ) or next((image for image in reversed(versions) if image.image_url), root)
            items.append(
                ImageLoopHistory(
                    **state.model_dump(),
                    versions=versions,
                    root_image=root,
                    preview_image_id=preview.id,
                    preview_image_url=preview.image_url,
                    create_time=row["loop_create_time"],
                    update_time=max(root.update_time, latest["loop_update_time"]),
                )
            )
        return items

    async def close_loop(
        self, image_id: int, user_id: int, action: ImageLoopExit, max_edits: int
    ) -> tuple[ImageLoop, bool]:
        async with self.pool.connection() as conn, conn.transaction():
            await self._lock_user(conn, user_id)
            root_image_id = await self._loop_root(conn, image_id, user_id)
            latest = await self._latest_loop(conn, root_image_id, user_id)
            if latest is None:
                raise ImageConflictError("这张图片还没有编辑记录")
            accepted = image_id if action == "ACCEPTED" else None
            if latest["loop_exit"]:
                if latest["loop_exit"] != action or latest["accepted_image_id"] != accepted:
                    raise ImageConflictError("这次图像创作已结束")
                return await self._loop_summary(conn, root_image_id, user_id, max_edits), False
            if accepted is not None:
                source = await self._source(conn, accepted, user_id)
                if source is None or source["image_url"] is None:
                    raise ImageConflictError("请选择一张已保存的图片")
            await conn.execute(
                """
                UPDATE image_edit_loop_versions
                SET loop_exit = %s, accepted_image_id = %s, update_by = %s,
                    update_time = CURRENT_TIMESTAMP
                WHERE root_image_id = %s AND user_id = %s AND version = %s
                """,
                (action, accepted, user_id, root_image_id, user_id, latest["version"]),
            )
            return await self._loop_summary(conn, root_image_id, user_id, max_edits), True

    async def _loop_root(
        self, conn: AsyncConnection[dict[str, Any]], image_id: int, user_id: int
    ) -> int:
        row = await (
            await conn.execute(
                """
                SELECT root_image_id FROM image_edit_loop_versions
                WHERE image_id = %s AND user_id = %s AND is_delete = 0
                """,
                (image_id, user_id),
            )
        ).fetchone()
        root_image_id = row["root_image_id"] if row else image_id
        await self._require_root(conn, root_image_id, user_id)
        return root_image_id

    async def _loop_summary(
        self,
        conn: AsyncConnection[dict[str, Any]],
        root_image_id: int,
        user_id: int,
        max_edits: int,
    ) -> ImageLoop:
        latest = await self._latest_loop(conn, root_image_id, user_id)
        state = self._loop_state(root_image_id, latest, max_edits)
        versions = await self._loop_versions(conn, [root_image_id], user_id)
        return ImageLoop(**state.model_dump(), versions=versions[root_image_id])

    @staticmethod
    def _loop_state(
        root_image_id: int, latest: dict[str, Any] | None, max_edits: int
    ) -> ImageLoopState:
        used_edits = latest["version"] if latest else 0
        status = latest["loop_exit"] if latest else None
        if status is None:
            if latest is not None and ImageStatus(latest["status"]).active:
                status = "PROCESSING"
            else:
                status = "EXHAUSTED" if used_edits >= max_edits else "OPEN"
        return ImageLoopState(
            root_image_id=root_image_id,
            used_edits=used_edits,
            max_edits=max_edits,
            status=status,
            accepted_image_id=latest["accepted_image_id"] if latest else None,
        )

    @staticmethod
    async def _loop_versions(
        conn: AsyncConnection[dict[str, Any]], root_image_ids: list[int], user_id: int
    ) -> defaultdict[int, list[ImageTask]]:
        rows = await (
            await conn.execute(
                SQL("""
                    SELECT {} FROM image_edit_loop_versions
                    WHERE user_id = %s AND root_image_id = ANY(%s) AND is_delete = 0
                    ORDER BY root_image_id, version
                """).format(_LOOP_COLUMNS),
                (user_id, root_image_ids),
            )
        ).fetchall()
        versions: defaultdict[int, list[ImageTask]] = defaultdict(list)
        for row in rows:
            versions[row["loop_root_image_id"]].append(ImageTask.model_validate(row))
        return versions

    async def list(
        self, user_id: int, query: ImageListQuery, *, created_since: datetime | None = None
    ) -> ImageTaskPage:
        filters: list[Composable] = [SQL("user_id = %s AND is_delete = 0")]
        params: list[object] = [user_id]
        order = SQL("id DESC")
        if created_since is not None:
            filters.append(SQL("create_time >= %s"))
            params.append(created_since)
            order = SQL("create_time DESC, id DESC")
        if query.status is not None:
            filters.append(SQL("status = %s"))
            params.append(query.status)
        if query.q is not None:
            matches = []
            if query.search_field != "content":
                matches.append(SQL("strpos(lower(title), lower(%s)) > 0"))
            if query.search_field != "title":
                matches.append(SQL("strpos(lower(prompt), lower(%s)) > 0"))
            filters.append(SQL("({})").format(SQL(" OR ").join(matches)))
            params.extend([query.q] * len(matches))
        where = SQL(" AND ").join(filters)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            count = await (
                await conn.execute(
                    SQL("SELECT COUNT(*) AS total FROM image_generation_tasks WHERE {}").format(
                        where
                    ),
                    params,
                )
            ).fetchone()
            rows = await (
                await conn.execute(
                    SQL("""
                        SELECT {} FROM image_generation_tasks WHERE {}
                        ORDER BY {} LIMIT %s OFFSET %s
                    """).format(_COLUMNS, where, order),
                    [*params, query.page_size, (query.page - 1) * query.page_size],
                )
            ).fetchall()
        total = count["total"] if count else 0
        return ImageTaskPage(
            items=[ImageTask.model_validate(row) for row in rows],
            total=total,
            page=query.page,
            page_size=query.page_size,
            total_pages=(total + query.page_size - 1) // query.page_size,
        )

    async def update_title(self, image_id: int, user_id: int, title: str) -> ImageTask | None:
        async with self.pool.connection() as conn:
            row = None
            for table, id_column, columns in (
                (_MAIN_TABLE, Identifier("id"), _COLUMNS),
                (_LOOP_TABLE, Identifier("image_id"), _LOOP_COLUMNS),
            ):
                row = await (
                    await conn.execute(
                        SQL("""
                            UPDATE {} SET title = %s, update_by = %s, update_time = CURRENT_TIMESTAMP
                            WHERE {} = %s AND user_id = %s AND is_delete = 0 AND title <> %s
                            RETURNING {}
                        """).format(table, id_column, columns),
                        (title, user_id, image_id, user_id, title),
                    )
                ).fetchone()
                if row:
                    break
        return ImageTask.model_validate(row) if row else await self.get(image_id, user_id)

    async def delete(self, image_id: int, user_id: int) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await self._lock_user(conn, user_id)
            row = await (
                await conn.execute(
                    """
                    SELECT status, is_loop FROM image_work_queue
                    WHERE id = %s AND user_id = %s AND is_delete = 0
                    """,
                    (image_id, user_id),
                )
            ).fetchone()
            if row is None:
                raise LookupError("图像不存在")
            if ImageStatus(row["status"]).active:
                raise ImageBusyError("图像任务完成后可以删除")
            table, id_column = (
                (_LOOP_TABLE, Identifier("image_id"))
                if row["is_loop"]
                else (_MAIN_TABLE, Identifier("id"))
            )
            await conn.execute(
                SQL("""
                UPDATE {}
                SET is_delete = 1, update_by = %s, update_time = CURRENT_TIMESTAMP
                WHERE {} = %s AND user_id = %s AND is_delete = 0
                """).format(table, id_column),
                (user_id, image_id, user_id),
            )

    async def claim(self, max_active: int, service_account: int) -> ImageClaim | None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(824567124)")
            candidate = await (
                await conn.execute(
                    SQL("""
                        SELECT id, is_loop, status FROM image_work_queue
                        WHERE is_delete = 0 AND {} AND next_poll_time <= CURRENT_TIMESTAMP
                          AND (status <> 'QUEUED' OR (
                            SELECT COUNT(*) FROM image_work_queue WHERE is_delete = 0 AND {}
                          ) < %s)
                        ORDER BY next_poll_time, id LIMIT 1
                    """).format(_ACTIVE, _SUBMITTED),
                    (max_active,),
                )
            ).fetchone()
            if candidate is None:
                return None
            table, id_column, columns = (
                (_LOOP_TABLE, Identifier("image_id"), _LOOP_COLUMNS)
                if candidate["is_loop"]
                else (_MAIN_TABLE, Identifier("id"), _COLUMNS)
            )
            row = await (
                await conn.execute(
                    SQL("""
                        UPDATE {}
                        SET status = CASE WHEN status = 'QUEUED' THEN 'SUBMITTING' ELSE status END,
                            provider_deadline = COALESCE(provider_deadline, CURRENT_TIMESTAMP + INTERVAL '23 hours'),
                            next_poll_time = CURRENT_TIMESTAMP + INTERVAL '2 minutes',
                            lease_until = CURRENT_TIMESTAMP + INTERVAL '2 minutes',
                            lease_version = lease_version + 1, update_by = %s,
                            update_time = CURRENT_TIMESTAMP
                        WHERE {} = %s AND status = %s AND is_delete = 0
                            AND next_poll_time <= CURRENT_TIMESTAMP
                        RETURNING {}, %s::varchar AS previous_status, provider_task_id, provider_image_url,
                            provider_deadline, source_urls, lease_version, failure_count
                    """).format(table, id_column, columns),
                    (service_account, candidate["id"], candidate["status"], candidate["status"]),
                )
            ).fetchone()
        return ImageClaim.model_validate(row) if row else None

    async def settle(
        self, claim: ImageClaim, progress: ImageProgress, service_account: int
    ) -> bool:
        table, id_column = (
            (_LOOP_TABLE, Identifier("image_id"))
            if claim.loop_version is not None
            else (_MAIN_TABLE, Identifier("id"))
        )
        async with self.pool.connection() as conn:
            result = await conn.execute(
                SQL("""
                UPDATE {}
                SET status = %s, provider_task_id = COALESCE(%s, provider_task_id),
                    provider_request_id = COALESCE(%s, provider_request_id), provider_image_url = %s,
                    image_url = %s, qiniu_object_key = %s, error_code = %s, failure_count = %s,
                    next_poll_time = CURRENT_TIMESTAMP + make_interval(secs => %s),
                    lease_until = NULL, update_by = %s, update_time = CURRENT_TIMESTAMP
                WHERE {} = %s AND lease_version = %s AND lease_until IS NOT NULL AND is_delete = 0
                """).format(table, id_column),
                (
                    progress.status,
                    progress.provider_task_id,
                    progress.provider_request_id,
                    progress.provider_image_url,
                    progress.image_url,
                    progress.qiniu_object_key,
                    progress.error_code,
                    progress.failure_count,
                    progress.delay_seconds,
                    service_account,
                    claim.id,
                    claim.lease_version,
                ),
            )
        return result.rowcount == 1
