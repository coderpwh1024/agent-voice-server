"""Connect to the configured Bailian Amap Maps MCP service."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.core.settings import Settings

logger = logging.getLogger(__name__)


async def _check_response(response: httpx2.Response) -> None:
    if response.request.method != "POST" or not response.headers.get(
        "content-type", ""
    ).lower().startswith("application/json"):
        return
    await response.aread()
    try:
        payload = response.json()
    except ValueError:
        return
    if (
        not isinstance(payload, dict)
        or "jsonrpc" in payload
        or payload.get("status") not in (0, "0")
    ):
        return
    code = str(payload.get("infocode", ""))
    if len(code) != 5 or not code.isascii() or not code.isdigit():
        return
    logger.warning(
        "Bailian Amap MCP provider failure http_status=%s provider_code=%s",
        response.status_code,
        code,
    )
    raise RuntimeError(f"Bailian Amap MCP provider error: {code}")


class AmapMcpService:
    def __init__(self, config: Settings) -> None:
        config.require_amap_mcp_configuration()
        url = config.AMAP_MCP_URL
        key = (
            config.AMAP_MCP_API_KEY
            if config.AMAP_MCP_API_KEY is not None
            else config.DASHSCOPE_API_KEY
        )
        if url is None or key is None:
            raise ValueError("Amap MCP is not configured")
        self.url = url
        self.api_key = key.get_secret_value()
        self.timeout = config.AMAP_MCP_TIMEOUT_SECONDS

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Client]:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx2.Timeout(self.timeout, read=300),
            follow_redirects=False,
            event_hooks={"response": [_check_response]},
        ) as http_client:
            async with Client(
                streamable_http_client(self.url, http_client=http_client),
                read_timeout_seconds=self.timeout,
                mode="legacy",
            ) as client:
                yield client
