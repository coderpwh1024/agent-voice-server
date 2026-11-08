import importlib
import json
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import MemorySaver

from app.api.chats import message_generator
from app.schemas import StreamInput


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", ["mem0-chatbot", "chatbot"])
@pytest.mark.parametrize("stream_tokens", [True, False])
async def test_main_agent_stream_keeps_tokens_messages_and_done(
    monkeypatch, agent_id, stream_tokens
):
    module = importlib.import_module("app.agents.mem0_chat")
    monkeypatch.setattr(module, "get_mem0_service", lambda: None)
    monkeypatch.setattr(
        module, "get_model", lambda *args, **kwargs: FakeListChatModel(responses=["你好。"])
    )
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    with patch("app.api.chats.get_agent", return_value=graph):
        chunks = [
            chunk
            async for chunk in message_generator(
                StreamInput(message="你好", user_id="7", stream_tokens=stream_tokens),
                agent_id=agent_id,
            )
        ]
    assert chunks[-1] == "data: [DONE]\n\n"
    events = [json.loads(chunk.removeprefix("data: ")) for chunk in chunks[:-1]]
    assert not any(event["type"] == "error" for event in events)
    messages = [
        event["content"]
        for event in events
        if event["type"] == "message" and event["content"]["type"] == "ai"
    ]
    progress = [
        event["content"]["custom_data"]
        for event in events
        if event["type"] == "message" and event["content"]["type"] == "custom"
    ]
    assert progress[-1]["status"] == "succeeded"
    assert progress[-1]["run_id"] == messages[0]["run_id"]
    assert len(messages) == 1
    assert messages[0]["type"] == "ai"
    assert messages[0]["content"] == "你好。"
    assert messages[0]["run_id"]
    assert not messages[0]["tool_calls"]
    tokens = "".join(event["content"] for event in events if event["type"] == "token")
    assert tokens == ("你好。" if stream_tokens else "")
