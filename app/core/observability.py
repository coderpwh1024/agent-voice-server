import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse, get_client
from langfuse.langchain import CallbackHandler

from app.core.settings import settings


def _langfuse_project_key() -> str | None:
    if not settings.LANGFUSE_TRACING or not settings.LANGFUSE_SECRET_KEY:
        return None
    if settings.LANGFUSE_PUBLIC_KEY:
        return settings.LANGFUSE_PUBLIC_KEY.get_secret_value() or None
    return None


@asynccontextmanager
async def langfuse_lifespan() -> AsyncIterator[None]:
    """Initialize the process-wide project after Nacos applies its settings."""
    public_key = _langfuse_project_key()
    secret_key = settings.LANGFUSE_SECRET_KEY
    if public_key is None or secret_key is None:
        yield
        return

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key.get_secret_value(),
        base_url=settings.LANGFUSE_HOST,
    )
    try:
        yield
    finally:
        await asyncio.to_thread(client.flush)


def langfuse_callbacks() -> list[BaseCallbackHandler]:
    public_key = _langfuse_project_key()
    return [CallbackHandler(public_key=public_key)] if public_key else []


async def langfuse_connected() -> bool:
    public_key = _langfuse_project_key()
    if public_key is None:
        return False
    return await asyncio.to_thread(get_client(public_key=public_key).auth_check)
