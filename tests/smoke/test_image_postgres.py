import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier

from app.core.database import get_postgres_connection_string
from app.core.settings import Settings
from app.models.image import (
    ImageBusyError,
    ImageClaim,
    ImageConflictError,
    ImageEditLimitError,
    ImageInputError,
    ImageProgress,
)
from app.repositories.files import FileRepository
from app.repositories.image import ImageRepository
from app.schemas.image import ImageCreate, ImageListQuery, ImageMetadataUpdate, ImageStatus
from app.services.files import FileService
from app.services.image.providers import ImageProviderResult, ImageStorageError
from app.services.image.service import ImageService

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[ImageRepository]:
    schema = "image_smoke_" + uuid4().hex
    conninfo = get_postgres_connection_string()
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
        await conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
    repository = ImageRepository(make_conninfo(conninfo, options=f"-csearch_path={schema}"))
    try:
        await repository.open()
        yield repository
    finally:
        await repository.close()
        async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
            await conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))


def _service(repo: ImageRepository, provider: AsyncMock, storage: AsyncMock) -> ImageService:
    config = Settings(_env_file=None, USE_FAKE_MODEL=True, IMAGE_MAX_ACTIVE_TASKS_PER_USER=1)
    return ImageService(repo, provider, storage, None, config)


async def _advance_clock(repo: ImageRepository, claim: ImageClaim) -> None:
    async with repo.pool.connection() as conn:
        await conn.execute(
            """
            UPDATE image_generation_tasks SET next_poll_time = CURRENT_TIMESTAMP - INTERVAL '1 second',
                lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second',
                update_by = 0, update_time = CURRENT_TIMESTAMP WHERE id = %s
            """,
            (claim.id,),
        )


async def test_image_claims_cap_concurrency_and_fence_expired_worker(repo: ImageRepository) -> None:
    provider, storage = AsyncMock(), AsyncMock()
    service = _service(repo, provider, storage)
    body = ImageCreate(prompt="山谷")
    first, replay = await asyncio.gather(
        service.create(12, "first", body, voice_edit_limit=5),
        service.create(12, "first", body, voice_edit_limit=5),
    )
    assert first.id == replay.id
    with pytest.raises(ImageConflictError, match="幂等键"):
        await service.create(12, "first", ImageCreate(prompt="海边"))
    with pytest.raises(ImageBusyError):
        await service.create(12, "second", body)
    other = await service.create(13, "first", body)
    with pytest.raises(LookupError):
        await service.query(first.id, 13)
    with pytest.raises(ImageBusyError):
        await service.delete(first.id, 12)

    first_claim, second_claim = await asyncio.gather(repo.claim(1, 0), repo.claim(1, 0))
    claim = first_claim or second_claim
    assert claim is not None and claim.id == first.id
    assert (first_claim is None) != (second_claim is None)
    assert claim.previous_status == ImageStatus.QUEUED
    await _advance_clock(repo, claim)
    recovered = await repo.claim(1, 0)
    assert recovered is not None and recovered.id == claim.id
    assert recovered.previous_status == ImageStatus.SUBMITTING
    stale_progress = ImageProgress(
        status=ImageStatus.PENDING, provider_task_id="late-vendor-response"
    )
    assert not await repo.settle(claim, stale_progress, 0)
    await service._advance(recovered)
    provider.create.assert_not_awaited()
    assert (await service.query(first.id, 12)).status == ImageStatus.SUBMISSION_UNKNOWN

    await service.delete(first.id, 12)
    with pytest.raises(ImageConflictError, match="已删除"):
        await service.create(12, "first", body, voice_edit_limit=5)
    assert (await repo.claim(1, 0)).id == other.id


async def test_image_worker_retries_archival_without_resubmitting(repo: ImageRepository) -> None:
    provider, storage = AsyncMock(), AsyncMock()
    service = _service(repo, provider, storage)
    short_url = "https://dashscope-result.oss-cn-shenzhen.aliyuncs.com/file.png?Expires=10"
    permanent_url = "https://cdn.example.com/images/qwen/task-image.png"
    provider.create.return_value = ImageProviderResult.model_validate(
        {"output": {"task_id": "task-image", "task_status": "PENDING"}}
    )
    provider.query.return_value = ImageProviderResult.model_validate(
        {
            "output": {
                "task_id": "task-image",
                "task_status": "SUCCEEDED",
                "choices": [{"message": {"content": [{"image": short_url}]}}],
            }
        }
    )
    storage.archive.side_effect = [
        ImageStorageError("temporary"),
        ("images/qwen/task-image.png", permanent_url),
    ]
    task = await service.create(12, "worker", ImageCreate(prompt="月光"))
    background = asyncio.create_task(service.run())
    try:
        async with asyncio.timeout(25):
            while True:
                current = await repo.get(task.id, 12)
                assert current is not None
                if current.status == ImageStatus.SUCCEEDED:
                    break
                assert current.status.active
                assert current.image_url is None
                await asyncio.sleep(0.1)
        assert str(current.image_url) == permanent_url
        assert provider.create.await_count == provider.query.await_count == 1
        assert storage.archive.await_count == 2
        storage.archive.assert_awaited_with("task-image", short_url)
        async with repo.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT provider_image_url FROM image_generation_tasks WHERE id = %s",
                    (task.id,),
                )
            ).fetchone()
        assert row is not None and row["provider_image_url"] is None
    finally:
        background.cancel()
        with suppress(asyncio.CancelledError):
            await background


async def test_image_edit_chain_boundary_and_titles(repo: ImageRepository) -> None:
    service = _service(repo, AsyncMock(), AsyncMock())
    old_id = None
    for turn in range(6):
        task = await service.create(
            12,
            f"voice-{turn}",
            ImageCreate(prompt="重绘山谷"),
            source_image_id=old_id,
            voice_edit_limit=5,
        )
        assert task.source_image_id == old_id
        claim = await repo.claim(1, 0)
        assert claim is not None and claim.id == task.id
        assert await repo.settle(
            claim,
            ImageProgress(
                status=ImageStatus.SUCCEEDED,
                provider_task_id=f"task-{turn}",
                qiniu_object_key=f"images/qwen/task-{turn}.png",
                image_url=f"https://cdn.example.com/images/qwen/task-{turn}.png",
            ),
            0,
        )
        old_id = task.id

    with pytest.raises(ImageEditLimitError):
        await service.create(
            12, "voice-6", ImageCreate(prompt="上色"), source_image_id=old_id, voice_edit_limit=5
        )
    with pytest.raises(LookupError):
        await service.create(13, "voice-5", ImageCreate(prompt="上色"), source_image_id=old_id)

    assert old_id is not None
    await service.update_metadata(old_id, 12, ImageMetadataUpdate(title="New title 中"))
    page = await service.list_tasks(12, ImageListQuery(q="NEW TITLE 中"))
    assert page.total == 1 and page.items[0].id == old_id
    await service.delete(old_id, 12)
    assert (await service.list_tasks(12, ImageListQuery())).total == 5


async def test_uploaded_references_preserve_order_and_user_ownership(repo: ImageRepository) -> None:
    files = FileRepository(repo.pool.conninfo)
    try:
        await files.open()
        config = Settings(_env_file=None, USE_FAKE_MODEL=True)
        uploads = FileService(files, AsyncMock(), 10 * 1024 * 1024)
        provider, archive = AsyncMock(), AsyncMock()
        service = ImageService(repo, provider, archive, uploads, config)
        first = await files.create(
            12,
            "https://cdn.example.com/first.png",
            "images",
            "first",
            "first.png",
            "image/png",
            100,
        )
        second = await files.create(
            12,
            "https://cdn.example.com/second.jpg",
            "images",
            "second",
            "second.jpg",
            "image/jpeg",
            100,
        )
        invalid = await files.create(
            12, "https://cdn.example.com/data.txt", "images", "data", "data.txt", "text/plain", 100
        )
        body = ImageCreate(prompt="组合两张图片", source_file_ids=[second.id, first.id])
        with pytest.raises(LookupError):
            await service.create(13, "references", body)
        with pytest.raises(ImageInputError):
            await service.create(
                12, "references", ImageCreate(prompt="美化", source_file_ids=[invalid.id])
            )
        task = await service.create(12, "references", body)
        claim = await repo.claim(1, 0)
        assert claim is not None and claim.id == task.id
        assert claim.source_file_ids == [second.id, first.id]
        assert claim.source_urls == [str(second.url), str(first.url)]
    finally:
        await files.close()
