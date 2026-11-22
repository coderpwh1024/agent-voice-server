"""DashScope asynchronous requests and verified Qiniu image storage."""

import asyncio
import logging
import re
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field, HttpUrl, ValidationError

from app.core.settings import Settings
from app.models.image import ImageClaim
from app.services.files import FileUploadError, QiniuFileStorage

logger = logging.getLogger(__name__)

_ProviderID = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")]
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_OSS_HOST = re.compile(r"(?:[a-z0-9-]+\.)+oss-[a-z0-9-]+\.aliyuncs\.com\Z")


class ImageProviderError(RuntimeError):
    def __init__(self, *, uncertain: bool = False) -> None:
        super().__init__("百炼图像接口暂不可用")
        self.uncertain = uncertain


class ImageStorageError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class _ImageContent(BaseModel):
    image: HttpUrl | None = None


class _ProviderMessage(BaseModel):
    content: list[_ImageContent]


class _ProviderChoice(BaseModel):
    message: _ProviderMessage


class _ProviderOutput(BaseModel):
    task_id: _ProviderID
    task_status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"]
    choices: list[_ProviderChoice] = Field(default_factory=list)


class ImageProviderResult(BaseModel):
    output: _ProviderOutput
    request_id: _ProviderID | None = None

    @property
    def image_url(self) -> str | None:
        for choice in self.output.choices:
            for content in choice.message.content:
                if content.image is not None:
                    return str(content.image)
        return None


class QwenImageClient:
    def __init__(self, client: httpx.AsyncClient, config: Settings) -> None:
        self.client = client
        self.base_url = config.IMAGE_API_BASE_URL.rstrip("/")
        key = config.IMAGE_API_KEY or config.DASHSCOPE_API_KEY
        if key is None:
            raise ValueError("图像生成需要百炼 API Key")
        self.headers = {"Authorization": f"Bearer {key.get_secret_value()}"}

    async def create(self, claim: ImageClaim) -> ImageProviderResult:
        content = [{"image": url} for url in claim.source_urls]
        content.append({"text": claim.prompt})
        parameters: dict[str, object] = {"n": 1, "watermark": False}
        if claim.size is not None:
            parameters["size"] = claim.size
        return await self._request(
            "POST",
            "/services/aigc/image-generation/generation",
            payload={
                "model": claim.model,
                "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": parameters,
            },
        )

    async def query(self, task_id: str) -> ImageProviderResult:
        result = await self._request("GET", f"/tasks/{quote(task_id, safe='')}")
        if result.output.task_id != task_id:
            logger.error("百炼图像查询任务不匹配 task_id=%s", task_id)
            raise ImageProviderError()
        return result

    async def _request(
        self, method: str, path: str, *, payload: dict[str, object] | None = None
    ) -> ImageProviderResult:
        submitting = method == "POST"
        headers = self.headers | ({"X-DashScope-Async": "enable"} if submitting else {})
        try:
            async with asyncio.timeout(30):
                response = await self.client.request(
                    method, self.base_url + path, headers=headers, json=payload
                )
        except (httpx.RequestError, TimeoutError) as exc:
            logger.error("百炼图像调用异常 method=%s error_type=%s", method, type(exc).__name__)
            raise ImageProviderError(uncertain=submitting) from None
        if not response.is_success:
            logger.error("百炼图像调用失败 method=%s status=%s", method, response.status_code)
            rejected = 400 <= response.status_code < 500 and response.status_code != 408
            raise ImageProviderError(uncertain=submitting and not rejected)
        try:
            result = ImageProviderResult.model_validate(response.json())
        except (ValidationError, ValueError):
            logger.error("百炼图像响应无法解析 method=%s", method)
            raise ImageProviderError(uncertain=submitting) from None
        logger.info(
            "百炼图像响应 method=%s task_id=%s request_id=%s status=%s",
            method,
            result.output.task_id,
            result.request_id,
            result.output.task_status,
        )
        return result


class QiniuImageStorage:
    def __init__(self, client: httpx.AsyncClient, storage: QiniuFileStorage) -> None:
        self.client = client
        self.storage = storage
        self.max_bytes = min(storage.max_bytes, 32 * 1024 * 1024)

    async def archive(self, task_id: str, source_url: str) -> tuple[str, str]:
        source = urlsplit(source_url)
        if (
            source.scheme != "https"
            or not _OSS_HOST.fullmatch(source.hostname or "")
            or source.username is not None
            or source.password is not None
            or source.port not in (443, None)
            or source.fragment
        ):
            raise ImageStorageError("百炼图像来源校验失败", retryable=False)
        key = f"images/qwen/{task_id}.png"
        try:
            if not await asyncio.to_thread(self._exists, key):
                try:
                    data = await self._download(source_url)
                except (httpx.HTTPError, TimeoutError):
                    await asyncio.to_thread(self._fetch, source_url, key)
                else:
                    await self.storage.upload(key, data, "image/png")
        except (httpx.HTTPError, OSError, FileUploadError, TimeoutError) as exc:
            logger.error("七牛图像转存异常 task_id=%s error_type=%s", task_id, type(exc).__name__)
            raise ImageStorageError("七牛图像转存暂不可用") from None
        return key, f"{self.storage.base_url}/{quote(key, safe='/')}"

    async def _download(self, url: str) -> bytes:
        data = bytearray()
        async with asyncio.timeout(35), self.client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(data) + len(chunk) > self.max_bytes:
                    raise ImageStorageError("百炼图像超出存储大小限制", retryable=False)
                data.extend(chunk)
        if not data.startswith(_PNG_SIGNATURE):
            raise ImageStorageError("百炼图像内容不是 PNG", retryable=False)
        return bytes(data)

    def _exists(self, key: str) -> bool:
        info, response = self.storage.manager.stat(self.storage.bucket_name, key)
        if response.status_code == 612:
            return False
        if response.status_code != 200 or not self._valid_object(info):
            raise ImageStorageError("七牛图像对象查询或校验失败")
        return True

    def _fetch(self, source_url: str, key: str) -> None:
        info, response = self.storage.manager.fetch(source_url, self.storage.bucket_name, key)
        if response.status_code != 200 or not self._valid_object(info) or info.get("key") != key:
            raise ImageStorageError("七牛未确认图像抓取成功")

    def _valid_object(self, value: object) -> bool:
        return (
            isinstance(value, dict)
            and bool(value.get("hash"))
            and isinstance(size := value.get("fsize"), int)
            and 0 < size <= self.max_bytes
            and value.get("mimeType") == "image/png"
        )
