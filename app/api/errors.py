import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import ServiceError
from app.schemas import ApiResponse

logger = logging.getLogger(__name__)


def _uses_protocol_response(path: str) -> bool:
    return (
        path == "/agui"
        or path.startswith("/agui/")
        or path == "/stream"
        or path.endswith("/stream")
        or (path.startswith("/voice/") and path.endswith("/ws"))
    )


async def api_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if _uses_protocol_response(request.url.path):
        return await http_exception_handler(request, exc)
    detail = exc.detail
    message = detail if isinstance(detail, str) else "Request failed"
    data = None if isinstance(detail, str) else detail
    response = ApiResponse[Any](code=exc.status_code, message=message, data=data)
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(response),
        headers=exc.headers,
    )


async def api_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    if _uses_protocol_response(request.url.path):
        return await request_validation_exception_handler(request, exc)
    response = ApiResponse[list[dict[str, Any]]](
        code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        message="Validation error",
        data=jsonable_encoder(exc.errors()),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=jsonable_encoder(response),
    )


async def api_unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled request error", exc_info=exc)
    if _uses_protocol_response(request.url.path):
        return PlainTextResponse("Internal Server Error", status_code=500)
    response = ApiResponse[Any](code=500, message="Internal server error", data=None)
    return JSONResponse(status_code=500, content=jsonable_encoder(response))


async def api_service_exception_handler(request: Request, exc: ServiceError):
    return await api_http_exception_handler(
        request, StarletteHTTPException(exc.status_code, exc.detail)
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.exception_handler(StarletteHTTPException)(api_http_exception_handler)
    app.exception_handler(RequestValidationError)(api_validation_exception_handler)
    app.exception_handler(ServiceError)(api_service_exception_handler)
    app.exception_handler(Exception)(api_unhandled_exception_handler)
