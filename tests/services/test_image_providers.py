import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr

from app.core.settings import Settings
from app.models.image import ImageClaim
from app.services import files as file_uploads
from app.services.files import QiniuFileStorage
from app.services.image.providers import (
    ImageProviderError,
    ImageStorageError,
    QiniuImageStorage,
    QwenImageClient,
)

OSS_URL = "https://dashscope-result.oss-cn-shenzhen.aliyuncs.com/asset.png?Expires=1"


def _claim() -> ImageClaim:
    now = datetime.now(UTC)
    return ImageClaim.model_validate(
        {
            "id": 1,
            "title": "山谷",
            "prompt": "增添晨雾",
            "model": "qwen-image-3.0",
            "status": "SUBMITTING",
            "source_image_id": 23,
            "source_file_ids": [7],
            "size": "1024*1024",
            "image_url": None,
            "error_code": None,
            "create_time": now,
            "update_time": now,
            "previous_status": "QUEUED",
            "provider_task_id": None,
            "provider_image_url": None,
            "provider_deadline": now + timedelta(hours=23),
            "source_urls": [
                "https://cdn.example.com/parent.png",
                "https://cdn.example.com/asset.png",
            ],
            "lease_version": 1,
            "failure_count": 0,
        }
    )


def _settings() -> Settings:
    return Settings(_env_file=None, USE_FAKE_MODEL=True, IMAGE_API_KEY=SecretStr("test-image-key"))


@pytest.mark.asyncio
async def test_qwen_async_generation_request_and_query_content() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-image-key"
        if request.method == "POST":
            assert request.url.path == "/api/v1/services/aigc/image-generation/generation"
            assert request.headers["X-DashScope-Async"] == "enable"
            assert json.loads(request.content) == {
                "model": "qwen-image-3.0",
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"image": "https://cdn.example.com/parent.png"},
                                {"image": "https://cdn.example.com/asset.png"},
                                {"text": "增添晨雾"},
                            ],
                        }
                    ]
                },
                "parameters": {"n": 1, "size": "1024*1024", "watermark": False},
            }
            return httpx.Response(
                200, json={"output": {"task_id": "task-1", "task_status": "PENDING"}}
            )
        assert request.url.path == "/api/v1/tasks/task-1"
        assert "X-DashScope-Async" not in request.headers
        return httpx.Response(
            200,
            json={
                "output": {
                    "task_id": "task-1",
                    "task_status": "SUCCEEDED",
                    "choices": [{"message": {"content": [{"image": OSS_URL}]}}],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = QwenImageClient(client, _settings())
        result = await provider.create(_claim())
        assert result.output.task_status == "PENDING"
        assert (await provider.query(result.output.task_id)).image_url == OSS_URL


@pytest.mark.parametrize("status, uncertain", [(400, False), (408, True), (502, True)])
@pytest.mark.asyncio
async def test_qwen_submission_error_distinguishes_ambiguous_acceptance(
    status: int, uncertain: bool
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    ) as client:
        provider = QwenImageClient(client, _settings())
        with pytest.raises(ImageProviderError) as failure:
            await provider.create(_claim())
        assert failure.value.uncertain is uncertain


@pytest.mark.parametrize("use_fetch", [False, True])
@pytest.mark.asyncio
async def test_qiniu_archive_confirms_png_and_recovers_existing_object(use_fetch: bool) -> None:
    body = b"\x89PNG\r\n\x1a\n\x00\x00"
    key = "images/qwen/task-1.png"
    properties = {"key": key, "hash": "stored-hash", "fsize": len(body), "mimeType": "image/png"}
    manager = Mock()
    manager.stat.side_effect = [
        ({}, SimpleNamespace(status_code=612)),
        (properties, SimpleNamespace(status_code=200)),
    ]
    manager.fetch.return_value = properties, SimpleNamespace(status_code=200)
    storage = SimpleNamespace(
        manager=manager,
        max_bytes=10000,
        bucket_name="image-assets",
        base_url="https://cdn.example.com",
        upload=AsyncMock(),
    )

    def source(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == OSS_URL
        return httpx.Response(403 if use_fetch else 200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(source)) as client:
        archive = QiniuImageStorage(client, storage)
        for _ in range(2):
            assert await archive.archive("task-1", OSS_URL) == (
                key,
                f"https://cdn.example.com/{key}",
            )
        with pytest.raises(ImageStorageError):
            await archive.archive("task-1", "https://untrusted.example.com/image.png")

    if use_fetch:
        manager.fetch.assert_called_once_with(OSS_URL, "image-assets", key)
        storage.upload.assert_not_awaited()
    else:
        manager.fetch.assert_not_called()
        storage.upload.assert_awaited_once_with(key, body, "image/png")


@pytest.mark.asyncio
async def test_image_qiniu_client_uses_https_for_management_and_upload(monkeypatch) -> None:
    key = "images/qwen/task-1.png"
    uploader = Mock()
    uploader.upload.return_value = (
        {"key": key, "hash": "stored-hash"},
        SimpleNamespace(status_code=200),
    )
    factory = Mock(return_value=uploader)
    monkeypatch.setattr(file_uploads, "FormUploader", factory)
    config = Settings(
        _env_file=None,
        USE_FAKE_MODEL=True,
        QINIU_ACCESS_KEY=SecretStr("example-ak"),
        QINIU_SECRET_KEY=SecretStr("example-sk"),
        QINIU_BUCKET_NAME="image-assets",
        QINIU_PUBLIC_BASE_URL="https://cdn.example.com",
    )
    storage = QiniuFileStorage(config, secure_transport=True)
    assert storage.manager.preferred_scheme == "https"
    assert await storage.upload(key, b"test", "image/png") == f"https://cdn.example.com/{key}"
    factory.assert_called_once_with("image-assets", preferred_scheme="https")
    assert uploader.upload.call_args.kwargs["key"] == key
