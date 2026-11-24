import logging

from fastapi import APIRouter

from app.core import settings
from app.core.observability import langfuse_connected
from app.schemas import ApiResponse, api_success

logger = logging.getLogger(__name__)


router = APIRouter()


@router.get("/health", response_model=ApiResponse[dict[str, str]])
async def health_check() -> ApiResponse[dict[str, str]]:
    """Health check endpoint."""

    health_status = {"status": "ok"}

    if settings.LANGFUSE_TRACING:
        try:
            health_status["langfuse"] = (
                "connected" if await langfuse_connected() else "disconnected"
            )
        except Exception as e:
            logger.warning("Langfuse connection check failed (%s)", type(e).__name__)
            health_status["langfuse"] = "disconnected"

    return api_success(health_status)
