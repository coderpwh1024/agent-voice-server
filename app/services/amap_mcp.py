"""Connect to the configured Bailian Amap Maps MCP service."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.core.settings import Settings


class AmapMcpService:
    def __init__(self, config: Settings) -> None:
        config.require_amap_mcp_configuration()
        url = config.AMAP_MCP_URL
        key = config.DASHSCOPE_API_KEY
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
        ) as http_client:
            async with Client(
                streamable_http_client(self.url, http_client=http_client),
                read_timeout_seconds=self.timeout,
            ) as client:
                yield client
