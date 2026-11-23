"""Image task intake and the durable DashScope/Qiniu state machine."""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime

from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout

from app.core.settings import Settings
from app.models.image import ImageClaim, ImageInputError, ImageProgress
from app.repositories.image import ImageRepository
from app.schemas.image import (
    ImageCreate,
    ImageListQuery,
    ImageMetadataUpdate,
    ImageStatus,
    ImageTask,
    ImageTaskPage,
)
from app.services.files import FileService
from app.services.image.providers import (
    ImageProviderError,
    ImageProviderResult,
    ImageStorageError,
    QiniuImageStorage,
    QwenImageClient,
)

logger = logging.getLogger(__name__)

_SOURCE_MIME_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/x-ms-bmp",
    "image/tiff",
}
_SOURCE_MAX_BYTES = 10 * 1024 * 1024
_POLL_SECONDS = 8
_IDLE_SECONDS = 2


class ImageService:
    def __init__(
        self,
        repo: ImageRepository,
        provider: QwenImageClient,
        storage: QiniuImageStorage,
        files: FileService | None,
        config: Settings,
    ) -> None:
        self.repo = repo
        self.provider = provider
        self.storage = storage
        self.files = files
        self.model = config.IMAGE_MODEL
        self.max_active = config.IMAGE_MAX_ACTIVE_TASKS
        self.max_active_per_user = config.IMAGE_MAX_ACTIVE_TASKS_PER_USER
        self.service_account = config.AUTH_SERVICE_ACCOUNT_ID
        self.wake = asyncio.Event()

    async def create(
        self,
        user_id: int,
        key: str,
        body: ImageCreate,
        *,
        source_image_id: int | None = None,
        voice_edit_limit: int | None = None,
    ) -> ImageTask:
        if source_image_id is not None and len(body.source_file_ids) > 2:
            raise ImageInputError("编辑原图时最多附加两张参考图片")
        request_hash = hashlib.sha256(
            json.dumps(
                {"body": body.model_dump(mode="json"), "source_image_id": source_image_id},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if replay := await self.repo.find_replay(user_id, key, request_hash):
            if replay.status.active:
                self.wake.set()
            return replay
        file_urls = await self._resolve_files(user_id, body.source_file_ids)
        task, created = await self.repo.enqueue(
            user_id,
            key,
            request_hash,
            body,
            model=self.model,
            file_urls=file_urls,
            source_image_id=source_image_id,
            voice_edit_limit=voice_edit_limit,
            max_active_per_user=self.max_active_per_user,
        )
        if created:
            logger.info(
                "图像任务已入队 image_id=%s source_image_id=%s model=%s references=%s",
                task.id,
                source_image_id,
                self.model,
                len(file_urls),
            )
        self.wake.set()
        return task

    async def _resolve_files(self, user_id: int, ids: list[int]) -> list[str]:
        if not ids:
            return []
        if self.files is None:
            raise ImageInputError("参考图片上传功能未启用")
        urls = []
        for file_id in ids:
            file = await self.files.get(file_id, user_id)
            if file.content_type not in _SOURCE_MIME_TYPES or file.size_bytes > _SOURCE_MAX_BYTES:
                raise ImageInputError("参考图必须是支持的图像格式且每张不超过 10 MB")
            urls.append(str(file.url))
        return urls

    async def query(self, image_id: int, user_id: int) -> ImageTask:
        task = await self.repo.get(image_id, user_id)
        if task is None:
            raise LookupError("图像不存在")
        if task.status.active:
            self.wake.set()
        return task

    async def list_tasks(
        self, user_id: int, query: ImageListQuery, *, created_since: datetime | None = None
    ) -> ImageTaskPage:
        return await self.repo.list(user_id, query, created_since=created_since)

    async def update_metadata(
        self, image_id: int, user_id: int, body: ImageMetadataUpdate
    ) -> ImageTask:
        task = await self.repo.update_title(image_id, user_id, body.title)
        if task is None:
            raise LookupError("图像不存在")
        logger.info("图像标题已更新 image_id=%s", image_id)
        return task

    async def delete(self, image_id: int, user_id: int) -> None:
        await self.repo.delete(image_id, user_id)
        logger.info("图像已逻辑删除 image_id=%s", image_id)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as workers:
            for index in range(min(self.max_active, 4)):
                workers.create_task(self._work(), name=f"image-worker-{index + 1}")

    async def _work(self) -> None:
        while True:
            task = None
            try:
                task = await self.repo.claim(self.max_active, self.service_account)
                if task is not None:
                    await self._advance(task)
                    continue
                async with asyncio.timeout(_IDLE_SECONDS):
                    await self.wake.wait()
                self.wake.clear()
            except TimeoutError:
                continue
            except (PostgresError, PoolTimeout) as exc:
                logger.error("图像队列存储异常 error_type=%s", type(exc).__name__)
                await asyncio.sleep(_IDLE_SECONDS)
            except Exception as exc:
                logger.error(
                    "图像工作者异常 image_id=%s error_type=%s",
                    task.id if task is not None else None,
                    type(exc).__name__,
                )
                await asyncio.sleep(_IDLE_SECONDS)

    async def _advance(self, task: ImageClaim) -> None:
        try:
            if task.previous_status == ImageStatus.QUEUED:
                progress = self._provider_progress(await self.provider.create(task))
            elif task.status == ImageStatus.SUBMITTING:
                progress = ImageProgress(
                    status=ImageStatus.SUBMISSION_UNKNOWN, error_code="SUBMISSION_UNCONFIRMED"
                )
            elif task.status == ImageStatus.STORING:
                progress = await self._store(task)
            else:
                progress = await self._poll(task)
        except ImageProviderError as exc:
            progress = ImageProgress(
                status=ImageStatus.SUBMISSION_UNKNOWN if exc.uncertain else ImageStatus.FAILED,
                error_code="SUBMISSION_UNCONFIRMED" if exc.uncertain else "PROVIDER_REJECTED",
            )
        if await self.repo.settle(task, progress, self.service_account):
            if progress.error_code or task.previous_status != progress.status:
                level = (
                    logging.ERROR
                    if progress.status in {ImageStatus.FAILED, ImageStatus.SUBMISSION_UNKNOWN}
                    else logging.INFO
                )
                logger.log(
                    level,
                    "图像任务状态 image_id=%s previous=%s status=%s code=%s",
                    task.id,
                    task.previous_status,
                    progress.status,
                    progress.error_code,
                )

    async def _poll(self, task: ImageClaim) -> ImageProgress:
        if datetime.now(UTC) >= task.provider_deadline or task.provider_task_id is None:
            return ImageProgress(status=ImageStatus.FAILED, error_code="PROVIDER_TASK_EXPIRED")
        try:
            result = await self.provider.query(task.provider_task_id)
        except ImageProviderError:
            return self._retry(task)
        if result.output.task_status == "UNKNOWN":
            if task.failure_count >= 8:
                return ImageProgress(
                    status=ImageStatus.FAILED, error_code="PROVIDER_TASK_NOT_FOUND"
                )
            return self._retry(task)
        return self._provider_progress(result)

    @staticmethod
    def _provider_progress(result: ImageProviderResult) -> ImageProgress:
        output = result.output
        fields = {"provider_task_id": output.task_id, "provider_request_id": result.request_id}
        if output.task_status in {"PENDING", "RUNNING"}:
            return ImageProgress(
                status=ImageStatus(output.task_status), delay_seconds=_POLL_SECONDS, **fields
            )
        if output.task_status == "SUCCEEDED" and (url := result.image_url):
            return ImageProgress(status=ImageStatus.STORING, provider_image_url=url, **fields)
        if output.task_status == "CANCELED":
            return ImageProgress(status=ImageStatus.CANCELED, **fields)
        return ImageProgress(
            status=ImageStatus.FAILED, error_code="PROVIDER_GENERATION_FAILED", **fields
        )

    async def _store(self, task: ImageClaim) -> ImageProgress:
        if not task.provider_task_id or not task.provider_image_url:
            return ImageProgress(status=ImageStatus.FAILED, error_code="PROVIDER_IMAGE_MISSING")
        try:
            key, url = await self.storage.archive(task.provider_task_id, task.provider_image_url)
        except ImageStorageError as exc:
            logger.warning("图像转存待处理 image_id=%s reason=%s", task.id, str(exc))
            if not exc.retryable:
                return ImageProgress(status=ImageStatus.FAILED, error_code="INVALID_PROVIDER_IMAGE")
            if datetime.now(UTC) >= task.provider_deadline:
                return ImageProgress(status=ImageStatus.FAILED, error_code="STORAGE_EXPIRED")
            return self._retry(task)
        return ImageProgress(status=ImageStatus.SUCCEEDED, qiniu_object_key=key, image_url=url)

    @staticmethod
    def _retry(task: ImageClaim) -> ImageProgress:
        return ImageProgress(
            status=task.status,
            provider_image_url=task.provider_image_url,
            failure_count=task.failure_count + 1,
            delay_seconds=min(5 * 2 ** min(task.failure_count, 5), 120),
        )
