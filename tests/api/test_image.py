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
    ImageLoop,
    ImageLoopHistory,
    ImageLoopHistoryPage,
    ImageLoopHistoryQuery,
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

    listing = client.get(
        "/images?page_size=10&status=SUCCEEDED&q=山谷&search_field=title", headers=headers
    )
    assert listing.status_code == 200 and listing.json()["data"]["items"] == [data]
    service.list_tasks.assert_awaited_once_with(
        12,
        ImageListQuery.model_validate(
            {"status": "SUCCEEDED", "q": "山谷", "page_size": 10, "search_field": "title"}
        ),
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


def test_loop_routes_use_authenticated_user_and_return_separate_versions(
    service: AsyncMock,
) -> None:
    client = TestClient(app)
    headers = _headers()
    revision = _task().model_copy(update={"loop_root_image_id": 40, "loop_version": 2})
    loop = ImageLoop(
        root_image_id=40,
        used_edits=2,
        max_edits=5,
        status="OPEN",
        accepted_image_id=None,
        versions=[revision],
    )
    service.query_loop.return_value = loop
    service.create.return_value = revision
    service.close_loop.return_value = loop.model_copy(
        update={"status": "ACCEPTED", "accepted_image_id": 42}
    )

    assert client.get("/images/40/loop").status_code == 401
    response = client.post("/images/40/loop/edits", headers=headers, json={"prompt": "换背景"})
    assert response.status_code == 202 and response.json()["data"]["loop_version"] == 2
    service.create.assert_awaited_once_with(
        12, "image-test", ImageCreate(prompt="换背景"), source_image_id=40, loop=True
    )
    assert client.get("/images/42/loop", headers=headers).json()["data"] == loop.model_dump(
        mode="json"
    )
    service.query_loop.assert_awaited_once_with(42, 12)
    response = client.post("/images/42/loop/close", headers=headers, json={"action": "ACCEPTED"})
    assert response.status_code == 200 and response.json()["data"]["accepted_image_id"] == 42
    service.close_loop.assert_awaited_once_with(42, 12, "ACCEPTED")


def test_loop_history_routes_return_public_records_with_default_page_size(
    service: AsyncMock,
) -> None:
    client = TestClient(app)
    headers = _headers()
    root = _task().model_copy(update={"id": 40})
    revision = _task().model_copy(
        update={"source_image_id": 40, "loop_root_image_id": 40, "loop_version": 1}
    )
    history = ImageLoopHistory(
        root_image_id=root.id,
        used_edits=1,
        max_edits=5,
        status="ACCEPTED",
        accepted_image_id=revision.id,
        versions=[revision],
        root_image=root,
        preview_image_id=revision.id,
        preview_image_url=revision.image_url,
        create_time=revision.create_time,
        update_time=revision.update_time,
    )
    service.list_loop_history.return_value = ImageLoopHistoryPage(
        items=[history], total=1, page=1, page_size=10, total_pages=1
    )
    service.get_loop_history.return_value = history

    for path in ("/images/loops", "/images/loops/40"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer trusted"}).status_code == 403
    service.list_loop_history.assert_not_awaited()
    service.get_loop_history.assert_not_awaited()

    listing = client.get("/images/loops", headers=headers)
    detail = client.get("/images/loops/40", headers=headers)
    assert listing.status_code == detail.status_code == 200
    assert listing.json()["data"] == {
        "items": [history.model_dump(mode="json")],
        "total": 1,
        "page": 1,
        "page_size": 10,
        "total_pages": 1,
    }
    assert detail.json()["data"] == listing.json()["data"]["items"][0]
    service.list_loop_history.assert_awaited_once_with(12, ImageLoopHistoryQuery())
    service.get_loop_history.assert_awaited_once_with(40, 12)

    response = client.get("/images/loops?page=2&page_size=3", headers=headers)
    assert response.status_code == 200
    service.list_loop_history.assert_awaited_with(12, ImageLoopHistoryQuery(page=2, page_size=3))
    service.get_loop_history.side_effect = LookupError()
    assert client.get("/images/loops/41", headers=headers).status_code == 404


@pytest.mark.parametrize("path", ["?page=0", "?page_size=101", "?user_id=14", "/0"])
def test_loop_history_rejects_invalid_query(service: AsyncMock, path: str) -> None:
    assert TestClient(app).get(f"/images/loops{path}", headers=_headers()).status_code == 422
    service.list_loop_history.assert_not_awaited()
    service.get_loop_history.assert_not_awaited()
