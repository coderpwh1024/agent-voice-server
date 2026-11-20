"""Connect directly to the official Amap Maps MCP service."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.core.settings import Settings

logger = logging.getLogger(__name__)


class AmapMcpPlaceholderKey(ValueError):
    """The Nacos Amap key still has its initial placeholder value."""


async def _check_response(response: httpx2.Response) -> None:
    if response.is_error:
        logger.error(
            "Amap MCP HTTP failure method=%s status=%s",
            response.request.method,
            response.status_code,
        )
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
    logger.error(
        "Amap MCP provider failure http_status=%s provider_code=%s",
        response.status_code,
        code,
    )
    raise RuntimeError(f"Amap MCP provider error: {code}")


class AmapMcpService:
    def __init__(self, config: Settings) -> None:
        config.require_amap_mcp_configuration()
        url = config.AMAP_MCP_URL
        key = config.AMAP_MAPS_API_KEY
        if not config.AMAP_MCP_ENABLED or url is None or key is None:
            raise ValueError("Amap MCP is not configured")
        self.url = url
        self._api_key = key
        self.timeout = config.AMAP_MCP_TIMEOUT_SECONDS

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Client]:
        key = self._api_key.get_secret_value().strip()
        if key.upper() == "XXXXXXX":
            raise AmapMcpPlaceholderKey
        logger.info("Connecting to official Amap MCP")
        async with httpx2.AsyncClient(
            params={"key": key},
            timeout=httpx2.Timeout(self.timeout, read=300),
            follow_redirects=False,
            max_redirects=0,
            event_hooks={"response": [_check_response]},
        ) as http_client:
            async with Client(
                streamable_http_client(self.url, http_client=http_client),
                read_timeout_seconds=self.timeout,
                mode="legacy",
            ) as client:
                logger.info("Official Amap MCP session initialized")
                yield client
