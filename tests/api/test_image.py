from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core import settings
from app.main import app
from app.models.image import ImageConflictError
from app.schemas.image import (
    ImageCreate,
    ImageListQuery,
    ImageMetadataUpdate,
    ImageTask,
    ImageTaskPage,
)
from app.services.auth import create_access_token
from app.services.image.service import ImageService


@pytest.fixture
def service(monkeypatch) -> AsyncMock:
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("trusted"))
    monkeypatch.setattr(settings, "APP_TOKEN_SECRET", SecretStr("t" * 32))
    mock = AsyncMock(spec=ImageService)
    monkeypatch.setattr(app.state, "image_service", mock, raising=False)
    return mock


def _headers() -> dict[str, str]:
    token, _ = create_access_token("12")
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": "image-test"}


def _task() -> ImageTask:
    return ImageTask.model_validate(
        {
            "id": 42,
            "title": "山谷",
            "prompt": "山谷里的晨雾",
            "model": "qwen-image-3.0",
            "status": "SUCCEEDED",
            "source_image_id": None,
            "source_file_ids": [7],
            "size": None,
            "image_url": "https://cdn.example.com/images/test.png",
            "error_code": None,
            "create_time": datetime.now(UTC),
            "update_time": datetime.now(UTC),
        }
    )


def test_rest_routes_bind_identity_and_return_public_image_metadata(service: AsyncMock) -> None:
    client = TestClient(app)
    headers = _headers()
    task = _task()
    service.create.return_value = task
    service.query.return_value = task
    service.list_tasks.return_value = ImageTaskPage(
        items=[task], total=1, page=1, page_size=10, total_pages=1
    )
    service.update_metadata.return_value = task

    body = {"prompt": task.prompt, "source_file_ids": [7]}
    create = client.post("/images", headers=headers, json=body)
    assert create.status_code == 202
    service.create.assert_awaited_once_with(12, "image-test", ImageCreate.model_validate(body))

    edit = client.post("/images/40/edits", headers=headers, json=body)
    assert edit.status_code == 202
    assert service.create.await_args.kwargs == {"source_image_id": 40}

    detail = client.get("/images/42", headers=headers)
    data = detail.json()["data"]
    assert data == task.model_dump(mode="json")
    service.query.assert_awaited_once_with(42, 12)

    listing = client.get("/images?page_size=10&status=SUCCEEDED&q=山谷", headers=headers)
    assert listing.status_code == 200 and listing.json()["data"]["items"] == [data]
    service.list_tasks.assert_awaited_once_with(
        12, ImageListQuery.model_validate({"status": "SUCCEEDED", "q": "山谷", "page_size": 10})
    )

    renamed = client.patch("/images/42", headers=headers, json={"title": " 山谷 "})
    assert renamed.status_code == 200
    service.update_metadata.assert_awaited_once_with(42, 12, ImageMetadataUpdate(title="山谷"))

    removed = client.delete("/images/42", headers=headers)
    assert removed.status_code == 200 and removed.json()["data"] == {"deleted": True}
    service.delete.assert_awaited_once_with(42, 12)


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": " "},
        {"prompt": "a\x00b"},
        {"prompt": "花", "size": "100*500"},
        {"prompt": "花", "source_file_ids": [1, 1]},
        {"prompt": "花", "source_file_ids": [True]},
        {"prompt": "花", "source_urls": ["https://example.com/input.png"]},
    ],
)
def test_invalid_create_does_not_enqueue(service: AsyncMock, body: dict[str, object]) -> None:
    assert TestClient(app).post("/images", headers=_headers(), json=body).status_code == 422
    service.create.assert_not_awaited()


def test_image_access_and_controlled_error_responses(service: AsyncMock) -> None:
    client = TestClient(app)
    assert client.get("/images").status_code == 401
    assert client.get("/images", headers={"Authorization": "Bearer trusted"}).status_code == 403
    service.list_tasks.assert_not_awaited()
    assert (
        client.post(
            "/images", json={"prompt": "花"}, headers={"Authorization": _headers()["Authorization"]}
        ).status_code
        == 422
    )
    service.create.assert_not_awaited()

    service.query.side_effect = LookupError()
    assert client.get("/images/12", headers=_headers()).status_code == 404
    service.create.side_effect = ImageConflictError("任务冲突")
    assert client.post("/images", headers=_headers(), json={"prompt": "花"}).status_code == 409
