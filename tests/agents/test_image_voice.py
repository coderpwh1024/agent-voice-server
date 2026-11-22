import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from app.models.image import ImageEditLimitError
from app.schemas.image import ImageStatus, ImageTask
from app.services.skills import image_voice

agent_module = importlib.import_module("app.agents.mem0_chat")


def operation(action: str, **args: str | int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "image_voice_operation", "args": {"action": action, **args}, "id": "call"}
        ],
    )


def image_task(image_id: int, status: ImageStatus, *, parent: int | None = None) -> ImageTask:
    return ImageTask(
        id=image_id,
        title="纸艺风景",
        prompt="测试图像",
        model="qwen-image-3.0",
        status=status,
        source_image_id=parent,
        source_file_ids=[],
        size=None,
        image_url="https://images.example.com/art.png" if status == ImageStatus.SUCCEEDED else None,
        error_code=None,
        create_time=datetime.now(UTC),
        update_time=datetime.now(UTC),
    )


def config(*, authenticated: str = "7", voice: bool = True) -> dict:
    return {
        "configurable": {
            "user_id": "7",
            "authenticated_user_id": authenticated,
            "thread_id": "voice-image-tests",
        },
        "metadata": {"execution_response_id": "response"} if voice else {},
    }


def user_input(text: str, turn: str = "one") -> dict:
    return {"messages": [HumanMessage(content=text, id=f"voice-input-{turn}")]}


@pytest.fixture
def setup(monkeypatch):
    model = Mock(ainvoke=AsyncMock(return_value=AIMessage(content="普通回复")))
    model.bind_tools.return_value = model
    service = Mock(create=AsyncMock(), query=AsyncMock())
    memory = Mock(search=AsyncMock(return_value=[]), remember=AsyncMock())
    monkeypatch.setattr(agent_module, "get_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(agent_module, "get_mem0_service", lambda: memory)
    monkeypatch.setattr(image_voice, "get_image_service", lambda: service)
    graph = agent_module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    return graph, model, service, memory


@pytest.mark.asyncio
async def test_voice_generation_is_idempotent_and_publishes_structured_task(setup):
    graph, model, service, memory = setup
    model.ainvoke.return_value = operation("create", prompt="纸艺风景，一座绿色小山")
    service.create.return_value = image_task(12, ImageStatus.QUEUED)
    request = user_input("请生成一张图像，纸艺风景里有一座绿色小山")
    events = [event async for event in graph.astream(request, config(), stream_mode="custom")]
    task_event = next(
        event.content[0] for event in events if event.content[0]["type"] == "image.task"
    )
    image = task_event["image"]
    assert image["id"] == 12
    assert image["status"] == "QUEUED" and image["image_url"] is None
    assert any(
        event.content[0].get("image") == image
        for event in events
        if event.content[0]["type"] == "execution.progress"
    )
    result = await graph.ainvoke(request, config())
    assert "图像编号 12" in result["messages"][0].content
    assert service.create.await_args_list[0] == service.create.await_args_list[1]
    assert service.create.await_args.args[0] == 7
    assert service.create.await_args.kwargs["voice_edit_limit"] == 5
    assert model.ainvoke.await_count == 2
    memory.remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_edits_use_saved_target_and_stop_when_limit_reached(setup):
    graph, model, service, _ = setup
    model.ainvoke.side_effect = [
        operation("detail", image_id=12),
        operation("edit", prompt="把背景换成蓝色"),
        operation("edit", prompt="把背景换成灰色"),
    ]
    service.query.return_value = image_task(12, ImageStatus.SUCCEEDED)
    service.create.side_effect = [
        image_task(13, ImageStatus.QUEUED, parent=12),
        ImageEditLimitError("limit"),
    ]
    detail = await graph.ainvoke(user_input("查一下十二号图片的状态"), config())
    assert detail["messages"][0].response_metadata["image"]["image_url"].endswith("/art.png")
    service.query.assert_awaited_once_with(12, 7)
    await graph.ainvoke(user_input("这张图的背景换成蓝色", "two"), config())
    stopped = await graph.ainvoke(user_input("这张图的背景换成灰色", "three"), config())
    assert "五次" in stopped["messages"][0].content
    assert stopped["messages"][0].response_metadata["execution"]["status"] == "failed"
    assert [call.kwargs["source_image_id"] for call in service.create.await_args_list] == [12, 13]
    assert model.ainvoke.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "settings", "decision", "expected"),
    [
        ("先不要生成图片", config(), operation("create", prompt="猫"), "clarification"),
        (
            "编辑图片 12 的背景",
            config(),
            operation("edit", image_id=99, prompt="猫"),
            "clarification",
        ),
        ("请生成一张图片", config(), AIMessage(content="虚构的图片"), "clarification"),
        ("请生成一张图片", config(), TimeoutError(), "timeout"),
        ("请生成一张图片", config(authenticated="8"), AIMessage(content="普通回复"), None),
        ("请生成一张图片", config(voice=False), AIMessage(content="普通回复"), None),
    ],
)
async def test_voice_requires_current_authorized_unambiguous_operation(
    setup, text, settings, decision, expected
):
    graph, model, service, _ = setup
    model.ainvoke.side_effect = [decision]
    result = await graph.ainvoke(user_input(text), settings)
    assert result["messages"][0].response_metadata.get("image_status") == expected
    service.create.assert_not_awaited()
    service.query.assert_not_awaited()
