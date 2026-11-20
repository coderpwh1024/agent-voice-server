import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from app.core import settings
from app.core.logging import ServiceLogFilter
from app.core.nacos import apply_remote_settings
from app.core.settings import Settings
from app.services import amap_mcp
from app.services.amap_mcp import AmapMcpService

AMAP_KEY = "amap-test-secret"


@pytest.fixture
def amap_transport_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "NACOS_ENABLED", True)
    monkeypatch.setattr(settings, "AMAP_MCP_ENABLED", True)
    monkeypatch.setattr(settings, "AMAP_MCP_URL", "https://mcp.amap.com/mcp")
    monkeypatch.setattr(settings, "AMAP_MAPS_API_KEY", SecretStr(AMAP_KEY))
    monkeypatch.setattr(settings, "DASHSCOPE_API_KEY", SecretStr("dashscope-test-secret"))


def _use_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    client_type = httpx2.AsyncClient

    def create_client(**kwargs: Any) -> httpx2.AsyncClient:
        return client_type(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(amap_mcp.httpx2, "AsyncClient", create_client)


@pytest.mark.asyncio
async def test_official_amap_streamable_http_uses_query_key_and_calls_weather(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = Settings(_env_file=None, NACOS_ENABLED=True, DASHSCOPE_API_KEY=SecretStr("chat-test"))
    apply_remote_settings(
        config, f"mcp:\n  amap_maps:\n    url: https://mcp.amap.com/mcp\n    key: {AMAP_KEY}\n"
    )
    methods = []
    requests = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.method != "POST":
            return httpx2.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        if method == "notifications/initialized":
            return httpx2.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "amap-maps", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "maps_weather", "inputSchema": {"type": "object"}}]}
        else:
            assert method == "tools/call"
            assert body["params"] == {"name": "maps_weather", "arguments": {"city": "杭州"}}
            message = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": '{"forecasts": []}'}]},
            }
            return httpx2.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=f"event: message\ndata: {json.dumps(message)}\n\n",
            )
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    _use_transport(monkeypatch, respond)
    with caplog.filtering(ServiceLogFilter()), caplog.at_level(logging.DEBUG):
        async with asyncio.timeout(1):
            async with AmapMcpService(config).connect() as client:
                listed = await client.list_tools()
                result = await client.call_tool("maps_weather", {"city": "杭州"})

    assert [tool.name for tool in listed.tools] == ["maps_weather"]
    assert not result.is_error
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert all(request.url == f"https://mcp.amap.com/mcp?key={AMAP_KEY}" for request in requests)
    assert all("Authorization" not in request.headers for request in requests)
    assert "Official Amap MCP session initialized" in caplog.text
    assert AMAP_KEY not in caplog.text
    assert "杭州" not in caplog.text


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
    with caplog.filtering(ServiceLogFilter()), caplog.at_level(logging.INFO):
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
    assert "response-secret" not in caplog.text
    assert AMAP_KEY not in caplog.text


@pytest.mark.asyncio
async def test_official_amap_does_not_follow_redirects(
    amap_transport_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return httpx2.Response(307, headers={"Location": "/redirected"})

    _use_transport(monkeypatch, respond)
    with pytest.raises(Exception) as caught:
        async with asyncio.timeout(1):
            async with AmapMcpService(settings).connect():
                pytest.fail("A redirect must not establish an MCP session")

    assert not isinstance(caught.value, TimeoutError)
    assert seen == ["/mcp"]
