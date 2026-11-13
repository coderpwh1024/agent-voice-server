import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from app.core import settings
from app.services import amap_mcp
from app.services.amap_mcp import AmapMcpService


@pytest.fixture
def amap_transport_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "NACOS_ENABLED", True)
    monkeypatch.setattr(settings, "AMAP_MCP_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "AMAP_MCP_URL",
        "https://llm-example.cn-beijing.maas.aliyuncs.com/api/v1/mcps/amap-maps/mcp",
    )
    monkeypatch.setattr(settings, "AMAP_MCP_API_KEY", SecretStr("bailian-test-secret"))


def _use_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    client_type = httpx2.AsyncClient

    def create_client(**kwargs: Any) -> httpx2.AsyncClient:
        return client_type(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(amap_mcp.httpx2, "AsyncClient", create_client)


@pytest.mark.asyncio
async def test_bailian_connection_initializes_without_an_unsupported_discovery_round(
    amap_transport_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    methods = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            return httpx2.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        if method == "notifications/initialized":
            return httpx2.Response(202)
        if method == "server/discover":
            return httpx2.Response(
                400,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
        result = (
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bailian-amap", "version": "1"},
            }
            if method == "initialize"
            else {"tools": [{"name": "maps_weather", "inputSchema": {"type": "object"}}]}
        )
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    _use_transport(monkeypatch, respond)
    async with asyncio.timeout(1):
        async with AmapMcpService(settings).connect() as client:
            result = await client.list_tools()

    assert [tool.name for tool in result.tools] == ["maps_weather"]
    assert methods == ["initialize", "notifications/initialized", "tools/list"]


@pytest.mark.asyncio
async def test_provider_failure_is_recorded_without_logging_response_secrets(
    amap_transport_settings: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "status": "0",
                "info": "INVALID_USER_KEY response-secret",
                "infocode": "10001",
            },
        )

    _use_transport(monkeypatch, respond)
    with caplog.at_level(logging.WARNING, logger="app.services.amap_mcp"):
        with pytest.raises(Exception) as caught:
            async with asyncio.timeout(1):
                async with AmapMcpService(settings).connect():
                    pytest.fail("A provider error must not establish an MCP session")

    assert not isinstance(caught.value, TimeoutError)
    messages = [
        record.getMessage() for record in caplog.records if record.name == "app.services.amap_mcp"
    ]
    assert any("10001" in message for message in messages)
    assert all("response-secret" not in message for message in messages)
    assert all("bailian-test-secret" not in message for message in messages)
    assert "response-secret" not in caplog.text
    assert "bailian-test-secret" not in caplog.text
