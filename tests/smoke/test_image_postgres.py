import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
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
    ImageLoopProcessingError,
    ImageProgress,
)
from app.repositories.files import FileRepository
from app.repositories.image import ImageRepository
from app.schemas.image import (
    ImageCreate,
    ImageListQuery,
    ImageLoopHistoryQuery,
    ImageMetadataUpdate,
    ImageStatus,
)
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
        service.create(12, "first", body, loop=True),
        service.create(12, "first", body, loop=True),
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
        await service.create(12, "first", body, loop=True)
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


async def test_standalone_image_edit_chain_and_titles(repo: ImageRepository) -> None:
    service = _service(repo, AsyncMock(), AsyncMock())
    old_id = None
    for turn in range(6):
        task = await service.create(
            12,
            f"voice-{turn}",
            ImageCreate(prompt="重绘山谷"),
            source_image_id=old_id,
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

    with pytest.raises(LookupError):
        await service.create(13, "voice-5", ImageCreate(prompt="上色"), source_image_id=old_id)

    assert old_id is not None
    await service.update_metadata(old_id, 12, ImageMetadataUpdate(title="New title 中"))
    page = await service.list_tasks(12, ImageListQuery(q="NEW TITLE 中"))
    assert page.total == 1 and page.items[0].id == old_id
    title = ImageListQuery(q="NEW TITLE 中", search_field="title")
    content = ImageListQuery(q="山谷", search_field="content", page_size=2)
    assert (await service.list_tasks(12, title)).total == 1
    assert (await service.list_tasks(12, ImageListQuery(q="山谷", search_field="title"))).total == 5
    assert (await service.list_tasks(12, content)).items[0].id == old_id
    assert (await service.list_tasks(12, content)).total == 6
    assert (await service.list_tasks(13, content)).total == 0
    await service.delete(old_id, 12)
    assert (await service.list_tasks(12, title)).total == 0
    assert (await service.list_tasks(12, ImageListQuery())).total == 5


async def _save_next(repo: ImageRepository, image_id: int) -> None:
    claim = await repo.claim(1, 0)
    assert claim is not None and claim.id == image_id
    assert await repo.settle(
        claim,
        ImageProgress(
            status=ImageStatus.SUCCEEDED,
            provider_task_id=f"task-{image_id}",
            qiniu_object_key=f"images/qwen/task-{image_id}.png",
            image_url=f"https://cdn.example.com/images/task-{image_id}.png",
        ),
        0,
    )


async def test_image_loop_counts_branches_and_keeps_versions_out_of_main_table(
    repo: ImageRepository,
) -> None:
    service = _service(repo, AsyncMock(), AsyncMock())
    body = ImageCreate(prompt="重绘山谷")
    root = await service.create(12, "initial", body, loop=True)
    await _save_next(repo, root.id)
    source = root.id
    versions = []
    for turn in range(1, 5):
        task = await service.create(
            12, f"edit-{turn}", body, source_image_id=source, loop=turn == 1
        )
        assert task.source_image_id == source
        assert task.loop_version == turn and task.loop_root_image_id == root.id
        await _save_next(repo, task.id)
        versions.append(task)
        source = root.id if turn == 2 else task.id

    last, rejected = await asyncio.gather(
        service.create(12, "fifth", body, source_image_id=root.id),
        service.create(12, "parallel", body, source_image_id=root.id),
        return_exceptions=True,
    )
    fifth_key = "fifth"
    if isinstance(last, ImageEditLimitError):
        last, rejected = rejected, last
        fifth_key = "parallel"
    assert isinstance(rejected, ImageEditLimitError)
    assert not isinstance(last, BaseException)
    assert last.loop_version == 5 and last.source_image_id == root.id
    replay = await service.create(12, fifth_key, body, source_image_id=root.id)
    assert replay.id == last.id
    await _save_next(repo, last.id)

    with pytest.raises(ImageEditLimitError):
        await service.create(12, "sixth", body, source_image_id=root.id, loop=True)
    with pytest.raises(LookupError):
        await service.create(13, "foreign", body, source_image_id=last.id, loop=True)
    with pytest.raises(LookupError):
        await service.query_loop(last.id, 13)
    summary = await service.query_loop(last.id, 12)
    assert summary.status == "EXHAUSTED" and summary.used_edits == 5
    assert [version.loop_version for version in summary.versions] == [1, 2, 3, 4, 5]
    assert all(version.image_url for version in summary.versions)
    async with repo.pool.connection() as conn:
        roots = await (await conn.execute("SELECT id FROM image_generation_tasks")).fetchall()
    assert roots == [{"id": root.id}]
    listing = await service.list_tasks(12, ImageListQuery(q="山谷", search_field="title"))
    assert [item.id for item in listing.items] == [root.id]
    await service.update_metadata(last.id, 12, ImageMetadataUpdate(title="New title 中"))
    assert (await service.query(last.id, 12)).title == "New title 中"
    await service.delete(last.id, 12)
    assert (await service.query_loop(root.id, 12)).used_edits == 5
    assert len((await service.query_loop(root.id, 12)).versions) == 4
    with pytest.raises(ImageConflictError, match="已删除"):
        await service.create(12, fifth_key, body, source_image_id=root.id)
    first = versions[0].id
    finished = await service.close_loop(first, 12, "ACCEPTED")
    assert finished.status == "ACCEPTED" and finished.accepted_image_id == first
    assert await service.close_loop(first, 12, "ACCEPTED") == finished


async def test_image_loop_stop_during_processing_and_recover_without_refund(
    repo: ImageRepository,
) -> None:
    service = _service(repo, AsyncMock(), AsyncMock())
    body = ImageCreate(prompt="山谷")
    root = await service.create(12, "root", body)
    await _save_next(repo, root.id)
    first = await service.create(12, "first", body, source_image_id=root.id, loop=True)
    with pytest.raises(ImageLoopProcessingError):
        await service.create(12, "other", body, source_image_id=root.id, loop=True)
    with pytest.raises(ImageBusyError):
        await service.create(12, "standalone", body)
    with pytest.raises(ImageConflictError, match="已保存"):
        await service.close_loop(first.id, 12, "ACCEPTED")
    claim = await repo.claim(1, 0)
    assert claim is not None and claim.id == first.id
    other_user = await service.create(13, "unrelated", body)
    assert await repo.claim(1, 0) is None
    assert await repo.settle(claim, ImageProgress(status=ImageStatus.SUBMISSION_UNKNOWN), 0)
    await _save_next(repo, other_user.id)

    second = await service.create(12, "second", body, source_image_id=root.id, loop=True)
    assert second.loop_version == 2
    finished = await service.close_loop(root.id, 12, "STOPPED")
    assert finished.status == "STOPPED" and finished.used_edits == 2
    await _save_next(repo, second.id)
    assert (await service.query(second.id, 12)).status == ImageStatus.SUCCEEDED
    assert (await service.query_loop(root.id, 12)).status == "STOPPED"
    with pytest.raises(ImageConflictError, match="已结束"):
        await service.create(12, "after-stop", body, source_image_id=root.id)


async def test_image_loop_history_paginates_owned_roots_and_preserves_visible_versions(
    repo: ImageRepository,
) -> None:
    service = _service(repo, AsyncMock(), AsyncMock())
    body = ImageCreate(prompt="山谷")
    root = await service.create(12, "root", body, loop=True)
    await _save_next(repo, root.id)
    earlier_loop_root = await service.create(12, "another-root", body)
    await _save_next(repo, earlier_loop_root.id)
    standalone = await service.create(12, "standalone", body, loop=True)
    await _save_next(repo, standalone.id)
    foreign_root = await service.create(13, "foreign-root", body)
    await _save_next(repo, foreign_root.id)
    foreign_edit = await service.create(
        13, "edit", body, source_image_id=foreign_root.id, loop=True
    )
    await _save_next(repo, foreign_edit.id)
    earlier_edit = await service.create(
        12, "earlier-edit", body, source_image_id=earlier_loop_root.id, loop=True
    )
    await _save_next(repo, earlier_edit.id)

    edit_body = ImageCreate(prompt="换成日出", size="1024*1024")
    first = await service.create(12, "first", edit_body, source_image_id=root.id, loop=True)
    await _save_next(repo, first.id)
    second = await service.create(12, "second", body, source_image_id=first.id)
    page = await service.list_loop_history(12, ImageLoopHistoryQuery(page_size=1))
    assert (page.total, page.page, page.page_size, page.total_pages) == (2, 1, 1, 2)
    history = page.items[0]
    assert history == await service.get_loop_history(root.id, 12)
    assert history.status == "PROCESSING" and history.used_edits == 2
    assert history.root_image.id == root.id and history.root_image.image_url
    assert history.create_time == first.create_time
    assert history.update_time >= second.update_time
    assert history.preview_image_id == first.id and history.preview_image_url
    assert [(version.id, version.source_image_id) for version in history.versions] == [
        (first.id, root.id),
        (second.id, first.id),
    ]
    assert history.versions[0].prompt == edit_body.prompt
    assert history.versions[0].size == edit_body.size
    assert history.versions[1].image_url is None

    next_page = await service.list_loop_history(12, ImageLoopHistoryQuery(page=2, page_size=1))
    assert [item.root_image_id for item in next_page.items] == [earlier_loop_root.id]
    empty = await service.list_loop_history(12, ImageLoopHistoryQuery(page=3, page_size=1))
    assert empty.items == [] and empty.total == 2
    assert (await service.list_loop_history(14, ImageLoopHistoryQuery())).total == 0
    for missing_root in (foreign_root.id, standalone.id, first.id):
        with pytest.raises(LookupError):
            await service.get_loop_history(missing_root, 12)

    claim = await repo.claim(1, 0)
    assert claim is not None and claim.id == second.id
    assert await repo.settle(
        claim, ImageProgress(status=ImageStatus.FAILED, error_code="PROVIDER_REJECTED"), 0
    )
    await service.close_loop(root.id, 12, "ACCEPTED")
    accepted = await service.get_loop_history(root.id, 12)
    assert accepted.status == "ACCEPTED" and accepted.accepted_image_id == root.id
    assert accepted.preview_image_id == root.id
    assert accepted.versions[-1].error_code == "PROVIDER_REJECTED"
    await service.delete(first.id, 12)
    remaining = await service.get_loop_history(root.id, 12)
    assert remaining.create_time == first.create_time and remaining.used_edits == 2
    assert [version.id for version in remaining.versions] == [second.id]
    await service.delete(root.id, 12)
    with pytest.raises(LookupError):
        await service.get_loop_history(root.id, 12)
    remaining_page = await service.list_loop_history(12, ImageLoopHistoryQuery())
    assert remaining_page.total == 1
    assert [item.root_image_id for item in remaining_page.items] == [earlier_loop_root.id]


async def test_recent_image_query_orders_by_creation_and_keeps_ownership(repo: ImageRepository):
    service = _service(repo, AsyncMock(), AsyncMock())
    service.max_active_per_user = 10
    cutoff = datetime.now(UTC) - timedelta(days=3)
    before_cutoff = cutoff - timedelta(microseconds=1)
    newer = cutoff + timedelta(seconds=1)
    expected_ids = []
    for key, owner, instant in [
        ("newer", 12, newer),
        ("boundary", 12, cutoff),
        ("older", 12, before_cutoff),
        ("deleted", 12, newer),
        ("another-user", 13, newer),
    ]:
        task = await service.create(owner, key, ImageCreate(prompt="山谷"))
        async with repo.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE image_generation_tasks
                SET create_time = %s, update_time = CURRENT_TIMESTAMP,
                    update_by = %s, is_delete = %s
                WHERE id = %s AND user_id = %s
                """,
                (instant, owner, int(key == "deleted"), task.id, owner),
            )
        if key in {"newer", "boundary"}:
            expected_ids.append(task.id)
    page = await service.list_tasks(12, ImageListQuery(page_size=6), created_since=cutoff)
    assert page.total == 2
    assert [task.id for task in page.items] == expected_ids


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
