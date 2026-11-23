"""PostgreSQL task ownership, reservations and crash-recoverable queue leases."""

import asyncio
import hashlib
from datetime import datetime
from typing import Any, LiteralString, cast

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.sql import SQL, Composable
from psycopg_pool import AsyncConnectionPool

from app.models import SQL_DIRECTORY
from app.models.image import (
    ImageBusyError,
    ImageClaim,
    ImageConflictError,
    ImageEditLimitError,
    ImageProgress,
)
from app.schemas.image import (
    ImageCreate,
    ImageListQuery,
    ImageModel,
    ImageStatus,
    ImageTask,
    ImageTaskPage,
)

_COLUMNS = SQL("""
    id, title, prompt, model, status, source_image_id, source_file_ids, size,
    image_url, error_code, create_time, update_time
""")
_ACTIVE = SQL("status IN ('QUEUED', 'SUBMITTING', 'PENDING', 'RUNNING', 'STORING')")
_SUBMITTED = SQL("status IN ('SUBMITTING', 'PENDING', 'RUNNING')")


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
        ddl = await asyncio.to_thread(
            SQL_DIRECTORY.joinpath("create_image_generation_tasks.sql").read_text,
            encoding="utf-8",
        )
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '10s'")
            await conn.execute("SELECT pg_advisory_xact_lock(824567123)")
            await conn.execute(SQL(cast(LiteralString, ddl)))

    async def close(self) -> None:
        await self.pool.close()

    async def find_replay(self, user_id: int, key: str, request_hash: str) -> ImageTask | None:
        async with self.pool.connection() as conn:
            return await self._find_replay(conn, user_id, key, request_hash)

    @staticmethod
    async def _find_replay(
        conn: AsyncConnection[dict[str, Any]], user_id: int, key: str, request_hash: str
    ) -> ImageTask | None:
        row = await (
            await conn.execute(
                SQL("""
                    SELECT {}, request_hash FROM image_generation_tasks
                    WHERE user_id = %s AND idempotency_key = %s AND is_delete = 0
                """).format(_COLUMNS),
                (user_id, key),
            )
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ImageConflictError("幂等键已用于其他图像请求")
        return ImageTask.model_validate(row)

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
        voice_edit_limit: int | None,
        max_active_per_user: int,
    ) -> tuple[ImageTask, bool]:
        async with self.pool.connection() as conn, conn.transaction():
            lock_id = int.from_bytes(
                hashlib.sha256(f"image-user:{user_id}".encode()).digest()[:8], "big", signed=True
            )
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
            if existing := await self._find_replay(conn, user_id, key, request_hash):
                return existing, False
            if voice_edit_limit is not None:
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
            depth = 0
            source_urls = file_urls
            if source_image_id is not None:
                source = await (
                    await conn.execute(
                        """
                        SELECT image_url, edit_depth FROM image_generation_tasks
                        WHERE id = %s AND user_id = %s AND is_delete = 0
                        FOR UPDATE
                        """,
                        (source_image_id, user_id),
                    )
                ).fetchone()
                if source is None:
                    raise LookupError("原图不存在")
                if source["image_url"] is None:
                    raise ImageConflictError("原图尚未成功生成")
                depth = source["edit_depth"] + 1
                if voice_edit_limit is not None and depth > voice_edit_limit:
                    raise ImageEditLimitError("已达到语音连续编辑次数上限")
                source_urls = [source["image_url"], *file_urls]
            active = await (
                await conn.execute(
                    SQL("""
                        SELECT COUNT(*) AS total FROM image_generation_tasks
                        WHERE user_id = %s AND is_delete = 0 AND {}
                    """).format(_ACTIVE),
                    (user_id,),
                )
            ).fetchone()
            if active and active["total"] >= max_active_per_user:
                raise ImageBusyError("当前图像任务较多，请等待已有任务完成")
            row = await (
                await conn.execute(
                    SQL("""
                        INSERT INTO image_generation_tasks (
                            user_id, title, prompt, model, size, source_image_id, source_file_ids,
                            source_urls, edit_depth, idempotency_key, request_hash, create_by, update_by
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING {}
                    """).format(_COLUMNS),
                    (
                        user_id,
                        body.title or body.prompt.splitlines()[0][:80],
                        body.prompt,
                        model,
                        body.size,
                        source_image_id,
                        body.source_file_ids,
                        source_urls,
                        depth,
                        key,
                        request_hash,
                        user_id,
                        user_id,
                    ),
                )
            ).fetchone()
            return ImageTask.model_validate(row), True

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
        return ImageTask.model_validate(row) if row else None

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
            row = await (
                await conn.execute(
                    SQL("""
                        UPDATE image_generation_tasks
                        SET title = %s, update_by = %s, update_time = CURRENT_TIMESTAMP
                        WHERE id = %s AND user_id = %s AND is_delete = 0 AND title <> %s
                        RETURNING {}
                    """).format(_COLUMNS),
                    (title, user_id, image_id, user_id, title),
                )
            ).fetchone()
        return ImageTask.model_validate(row) if row else await self.get(image_id, user_id)

    async def delete(self, image_id: int, user_id: int) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    """
                    SELECT status FROM image_generation_tasks
                    WHERE id = %s AND user_id = %s AND is_delete = 0 FOR UPDATE
                    """,
                    (image_id, user_id),
                )
            ).fetchone()
            if row is None:
                raise LookupError("图像不存在")
            if ImageStatus(row["status"]).active:
                raise ImageBusyError("图像任务完成后可以删除")
            await conn.execute(
                """
                UPDATE image_generation_tasks
                SET is_delete = 1, update_by = %s, update_time = CURRENT_TIMESTAMP
                WHERE id = %s AND user_id = %s AND is_delete = 0
                """,
                (user_id, image_id, user_id),
            )

    async def claim(self, max_active: int, service_account: int) -> ImageClaim | None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(824567124)")
            row = await (
                await conn.execute(
                    SQL("""
                        WITH candidate AS (
                            SELECT id AS candidate_id, status AS previous_status
                            FROM image_generation_tasks
                            WHERE is_delete = 0 AND {} AND next_poll_time <= CURRENT_TIMESTAMP
                              AND (status <> 'QUEUED' OR (
                                SELECT COUNT(*) FROM image_generation_tasks
                                WHERE is_delete = 0 AND {}
                              ) < %s)
                            ORDER BY next_poll_time, id
                            LIMIT 1 FOR UPDATE SKIP LOCKED
                        )
                        UPDATE image_generation_tasks
                        SET status = CASE WHEN previous_status = 'QUEUED' THEN 'SUBMITTING' ELSE status END,
                            provider_deadline = COALESCE(provider_deadline, CURRENT_TIMESTAMP + INTERVAL '23 hours'),
                            next_poll_time = CURRENT_TIMESTAMP + INTERVAL '2 minutes',
                            lease_until = CURRENT_TIMESTAMP + INTERVAL '2 minutes',
                            lease_version = lease_version + 1, update_by = %s,
                            update_time = CURRENT_TIMESTAMP
                        FROM candidate WHERE id = candidate_id
                        RETURNING {}, previous_status, provider_task_id, provider_image_url,
                            provider_deadline, source_urls, lease_version, failure_count
                    """).format(_ACTIVE, _SUBMITTED, _COLUMNS),
                    (max_active, service_account),
                )
            ).fetchone()
        return ImageClaim.model_validate(row) if row else None

    async def settle(
        self, claim: ImageClaim, progress: ImageProgress, service_account: int
    ) -> bool:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                """
                UPDATE image_generation_tasks
                SET status = %s, provider_task_id = COALESCE(%s, provider_task_id),
                    provider_request_id = COALESCE(%s, provider_request_id), provider_image_url = %s,
                    image_url = %s, qiniu_object_key = %s, error_code = %s, failure_count = %s,
                    next_poll_time = CURRENT_TIMESTAMP + make_interval(secs => %s),
                    lease_until = NULL, update_by = %s, update_time = CURRENT_TIMESTAMP
                WHERE id = %s AND lease_version = %s AND lease_until IS NOT NULL AND is_delete = 0
                """,
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
