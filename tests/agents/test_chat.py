import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.agents.chat import answer_chat
from app.core import settings
from app.services.skills.video_chat import VideoSkillSession

CONFIG = {"configurable": {"user_id": "7", "thread_id": "chat", "model": "qwen3.8-max"}}


def chat_model(response):
    model = Mock(ainvoke=AsyncMock(return_value=response))
    model.bind_tools.return_value = model
    return model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "enabled", "video_enabled", "expected_search"),
    [
        ("qwen3.8-max", True, False, True),
        ("qwen3.8-max", True, True, True),
        ("qwen3.8-max", False, False, False),
        ("gpt-5-nano", True, False, False),
    ],
)
async def test_bailian_search_is_scoped_to_main_answer(
    monkeypatch, model_name, enabled, video_enabled, expected_search
):
    monkeypatch.setattr(settings, "DASHSCOPE_ENABLE_SEARCH", enabled)
    monkeypatch.setattr(settings, "VIDEO_SKILL_ENABLED", video_enabled)
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "回答"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    model = ChatOpenAI(
        model=model_name,
        api_key="test-key",
        base_url="https://example.com/compatible-mode/v1",
        extra_body={"enable_thinking": False},
        http_async_client=client,
    )
    human = HumanMessage(content="查最新消息")
    video = VideoSkillSession(human, None, []) if video_enabled else None
    config = {"configurable": {"user_id": "7", "thread_id": "chat", "model": model_name}}
    async with client:
        reply = await answer_chat(model, [human], config, video=video)

    assert reply.content == "回答"
    assert len(requests) == 1
    request = requests[0]
    assert request["enable_thinking"] is False
    assert request.get("enable_search") is (True if expected_search else None)
    assert ("tools" in request) is video_enabled
    assert model.extra_body == {"enable_thinking": False}
    rules = "\n".join(
        message["content"] for message in request["messages"] if message["role"] == "system"
    )
    assert ("可以使用百炼内置联网搜索" in rules) is expected_search


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_research_enabled", [False, True])
async def test_plain_chat_has_no_research_tools_and_uses_runtime_date(
    monkeypatch, legacy_research_enabled
):
    monkeypatch.setattr(settings, "VIDEO_SKILL_ENABLED", False)
    monkeypatch.setattr(settings, "MEM0_RESEARCH_ENABLED", legacy_research_enabled)
    model = chat_model(AIMessage(content="暂时无法联网核实最新消息。"))
    prompt = [SystemMessage(content="角色规则"), HumanMessage(content="联网查最新消息")]
    reply = await answer_chat(model, prompt, CONFIG)
    assert reply.content == "暂时无法联网核实最新消息。"
    model.ainvoke.assert_awaited_once()
    model.bind_tools.assert_not_called()
    sent_prompt = model.ainvoke.await_args.args[0]
    runtime = next(message for message in sent_prompt if message.name == "runtime_context")
    assert datetime.now(UTC).date().isoformat() in runtime.content
    assert sent_prompt[-1] == prompt[-1]
    assert "research_status" not in reply.response_metadata
    assert len(prompt) == 2


@pytest.mark.asyncio
async def test_only_video_skill_is_offered_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_SKILL_ENABLED", True)
    monkeypatch.setattr(settings, "MEM0_RESEARCH_ENABLED", True)
    human = HumanMessage(content="你好")
    video = VideoSkillSession(human, None, [])
    model = chat_model(AIMessage(content="你好。"))
    reply = await answer_chat(model, [human], CONFIG, video=video)
    assert reply.content == "你好。"
    assert [tool["name"] for tool in model.bind_tools.call_args.args[0]] == ["load_video_skill"]


@pytest.mark.asyncio
async def test_model_without_tool_support_keeps_chat_available(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_SKILL_ENABLED", True)
    human = HumanMessage(content="你好")
    model = chat_model(AIMessage(content="你好。"))
    model.bind_tools.side_effect = NotImplementedError
    reply = await answer_chat(model, [human], CONFIG, video=VideoSkillSession(human, None, []))
    assert reply.content == "你好。"
    model.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["research_assistant", "send_email", "video_operation"])
async def test_unavailable_tool_calls_are_not_executed_or_returned(monkeypatch, tool_name):
    monkeypatch.setattr(settings, "VIDEO_SKILL_ENABLED", False)
    model = chat_model(
        AIMessage(content="", tool_calls=[{"name": tool_name, "args": {}, "id": "obsolete"}])
    )
    reply = await answer_chat(model, [HumanMessage(content="执行操作")], CONFIG)
    assert reply.response_metadata["tool_status"] == "invalid"
    assert reply.content and not reply.tool_calls
    model.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_failure_keeps_existing_error_semantics(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_SKILL_ENABLED", False)
    model = chat_model(AIMessage(content=""))
    model.ainvoke.side_effect = ConnectionError("provider unavailable")
    with pytest.raises(ConnectionError):
        await answer_chat(model, [HumanMessage(content="你好")], CONFIG)


@pytest.mark.asyncio
async def test_main_model_cancellation_propagates(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_SKILL_ENABLED", False)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def slow_reply(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model = chat_model(AIMessage(content=""))
    model.ainvoke.side_effect = slow_reply
    invocation = asyncio.create_task(answer_chat(model, [HumanMessage(content="你好")], CONFIG))
    async with asyncio.timeout(2):
        await started.wait()
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
    assert stopped.is_set()
