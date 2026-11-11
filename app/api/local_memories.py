"""Authenticated stateless proposals for phone-owned Markdown memories."""

import logging

from fastapi import APIRouter, HTTPException, Request

from app.core import settings
from app.schemas.api import ApiResponse, api_success
from app.schemas.local_memory import LocalMemoryExtractionInput, LocalMemoryExtractionResult
from app.services.auth import bind_user
from app.services.memory.local_extraction import extract_device_memories

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/local-memories", tags=["local-memories"])


@router.post("/extract", response_model=ApiResponse[LocalMemoryExtractionResult])
async def extract_local_memories(
    body: LocalMemoryExtractionInput, request: Request, user_id: str | None = None
) -> ApiResponse[LocalMemoryExtractionResult]:
    identity = request.state.principal
    if identity.admin and user_id is None:
        raise HTTPException(422, "user_id is required for trusted service credentials")
    bind_user(identity, user_id)
    if not settings.LOCAL_MEMORY_ENABLED:
        raise HTTPException(503, "Device memory is disabled")
    try:
        return api_success(await extract_device_memories(body))
    except TimeoutError:
        logger.warning("Local memory extraction timed out source_id=%s", body.source_id)
        raise HTTPException(
            504, "Memory proposals timed out; no device files were changed"
        ) from None
    except Exception as exc:
        logger.warning(
            "Local memory extraction failed source_id=%s error_type=%s",
            body.source_id,
            type(exc).__name__,
        )
        raise HTTPException(
            503, "Memory proposals unavailable; no device files were changed"
        ) from None
