"""Application user image API."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout

from app.api.dependencies import principal
from app.models.image import ImageConflictError, ImageInputError
from app.schemas import ApiResponse, api_success
from app.schemas.image import (
    ImageCreate,
    ImageListQuery,
    ImageLoop,
    ImageLoopClose,
    ImageLoopHistory,
    ImageLoopHistoryPage,
    ImageLoopHistoryQuery,
    ImageMetadataUpdate,
    ImageTask,
    ImageTaskPage,
)
from app.services.image.service import ImageService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/images", tags=["images"])

ImagePathID = Annotated[int, Path(gt=0, le=9223372036854775807)]
IdempotencyKey = Annotated[str, Header(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")]


def _user_id(request: Request) -> int:
    identity = principal(request)
    if identity.admin or identity.user_id is None:
        raise HTTPException(403, "图像接口需要应用用户令牌")
    if (
        not identity.user_id.isascii()
        or not identity.user_id.isdecimal()
        or len(identity.user_id) > 19
    ):
        raise HTTPException(401, "应用用户 ID 必须是正整数")
    user_id = int(identity.user_id)
    if not 0 < user_id <= 9223372036854775807:
        raise HTTPException(401, "应用用户 ID 超出范围")
    return user_id


def _service(request: Request) -> ImageService:
    service = getattr(request.app.state, "image_service", None)
    if service is None:
        raise HTTPException(503, "图像生成功能未启用")
    return service


@contextmanager
def _handle_errors() -> Iterator[None]:
    try:
        yield
    except LookupError:
        raise HTTPException(404, "图像或参考文件不存在") from None
    except ImageConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except ImageInputError as exc:
        raise HTTPException(422, str(exc)) from None
    except (PostgresError, PoolTimeout) as exc:
        logger.error("图像接口存储异常 error_type=%s", type(exc).__name__)
        raise HTTPException(503, "图像任务存储暂不可用") from None


@router.post("", response_model=ApiResponse[ImageTask], status_code=202)
async def create_image(
    body: ImageCreate, request: Request, idempotency_key: IdempotencyKey
) -> ApiResponse[ImageTask]:
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(await _service(request).create(user_id, idempotency_key, body))


@router.post("/{image_id}/edits", response_model=ApiResponse[ImageTask], status_code=202)
async def edit_image(
    image_id: ImagePathID, body: ImageCreate, request: Request, idempotency_key: IdempotencyKey
) -> ApiResponse[ImageTask]:
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(
            await _service(request).create(user_id, idempotency_key, body, source_image_id=image_id)
        )


@router.get("", response_model=ApiResponse[ImageTaskPage])
async def list_images(
    request: Request, query: Annotated[ImageListQuery, Query()]
) -> ApiResponse[ImageTaskPage]:
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(await _service(request).list_tasks(user_id, query))


@router.get("/loops", response_model=ApiResponse[ImageLoopHistoryPage])
async def list_image_loop_history(
    request: Request, query: Annotated[ImageLoopHistoryQuery, Query()]
) -> ApiResponse[ImageLoopHistoryPage]:
    """List the current user's image editing history, newest loop first."""
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(await _service(request).list_loop_history(user_id, query))


@router.get("/loops/{root_image_id}", response_model=ApiResponse[ImageLoopHistory])
async def get_image_loop_history(
    root_image_id: ImagePathID, request: Request
) -> ApiResponse[ImageLoopHistory]:
    """Get a recorded image editing loop by its root image ID."""
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(await _service(request).get_loop_history(root_image_id, user_id))


@router.get("/{image_id}/loop", response_model=ApiResponse[ImageLoop])
async def get_image_loop(image_id: ImagePathID, request: Request) -> ApiResponse[ImageLoop]:
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(await _service(request).query_loop(image_id, user_id))


@router.post("/{image_id}/loop/edits", response_model=ApiResponse[ImageTask], status_code=202)
async def edit_image_loop(
    image_id: ImagePathID, body: ImageCreate, request: Request, idempotency_key: IdempotencyKey
) -> ApiResponse[ImageTask]:
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(
            await _service(request).create(
                user_id, idempotency_key, body, source_image_id=image_id, loop=True
            )
        )


@router.post("/{image_id}/loop/close", response_model=ApiResponse[ImageLoop])
async def close_image_loop(
    image_id: ImagePathID, body: ImageLoopClose, request: Request
) -> ApiResponse[ImageLoop]:
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(await _service(request).close_loop(image_id, user_id, body.action))


@router.get("/{image_id}", response_model=ApiResponse[ImageTask])
async def get_image(image_id: ImagePathID, request: Request) -> ApiResponse[ImageTask]:
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(await _service(request).query(image_id, user_id))


@router.patch("/{image_id}", response_model=ApiResponse[ImageTask])
async def update_image(
    image_id: ImagePathID, body: ImageMetadataUpdate, request: Request
) -> ApiResponse[ImageTask]:
    user_id = _user_id(request)
    with _handle_errors():
        return api_success(await _service(request).update_metadata(image_id, user_id, body))


@router.delete("/{image_id}", response_model=ApiResponse[dict[str, bool]])
async def delete_image(image_id: ImagePathID, request: Request) -> ApiResponse[dict[str, bool]]:
    user_id = _user_id(request)
    with _handle_errors():
        await _service(request).delete(image_id, user_id)
    return api_success({"deleted": True})
