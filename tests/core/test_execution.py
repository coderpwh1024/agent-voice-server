import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage
from langgraph.store.memory import InMemoryStore

from app.schemas.execution import ExecutionConfirmation
from app.services.execution import ExecutionTrace, execution_history, progress, record_video_result


def trace(store, user="42", thread="thread"):
    return ExecutionTrace(
        {"configurable": {"user_id": user, "thread_id": thread}}, store, lambda _: None, "input"
    )


@pytest.mark.asyncio
async def test_progress_survives_restart_and_stays_user_and_thread_scoped():
    store = InMemoryStore()
    execution = trace(store)
    async with execution.bind():
        await progress("memory.search", "检索记忆")
        await progress("memory.search", "检索记忆", "warning", "暂不可用")
        await progress("chat.answer", "生成回复")
        response = AIMessage(content="你好")
        await execution.finish(response)
    rows = await execution_history(store, "42", "thread")
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert [step.status for step in rows[0].steps] == ["warning", "succeeded"]
    assert rows[0].text == response.content
    assert rows[0].message_id == response.id
    assert response.response_metadata["execution"]["sequence"] == rows[0].sequence
    assert not await execution_history(store, "43", "thread")
    assert not await execution_history(store, "42", "other")


@pytest.mark.asyncio
async def test_cancel_preserves_completed_side_effects_and_marks_inflight_unknown():
    store = InMemoryStore()
    execution = trace(store)
    with pytest.raises(asyncio.CancelledError):
        async with execution.bind():
            await progress("video.skill", "加载技能", "succeeded")
            await progress("video.submit", "提交视频")
            raise asyncio.CancelledError()
    rows = await execution_history(store, "42", "thread")
    assert rows[0].status == "cancelled"
    assert [step.status for step in rows[0].steps] == ["succeeded", "unknown"]


@pytest.mark.asyncio
async def test_stale_running_snapshot_and_expired_confirmation_do_not_spin_forever():
    store = InMemoryStore()
    execution = trace(store)
    await execution.step("chat.answer", "回答")
    execution.snapshot.updated_at = datetime.now(UTC) - timedelta(minutes=6)
    await store.aput(
        execution.namespace, execution.snapshot.run_id, execution.snapshot.model_dump(mode="json")
    )
    rows = await execution_history(store, "42", "thread")
    assert rows[0].status == "unknown"
    assert rows[0].steps[0].status == "unknown"
    execution.snapshot.status = "waiting_confirmation"
    execution.snapshot.confirmation = ExecutionConfirmation(
        confirmation_id="a" * 64, label="确认", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    await execution.publish()
    rows = await execution_history(store, "42", "thread")
    assert rows[0].confirmation is None
    assert rows[0].status == "cancelled"


@pytest.mark.asyncio
async def test_recovery_text_uses_only_public_text_blocks():
    execution = trace(InMemoryStore())
    response = AIMessage(
        content=[
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "text", "text": "public reply"},
        ]
    )
    await execution.finish(response)
    assert execution.snapshot.text == "public reply"
    assert "private reasoning" not in str(response.response_metadata["execution"])


@pytest.mark.parametrize("approved", ["yes", "true", 1, None])
def test_confirmation_requires_an_explicit_boolean(approved):
    from pydantic import ValidationError

    from app.schemas.execution import MemoryConfirmation

    with pytest.raises(ValidationError):
        MemoryConfirmation.model_validate({"confirmation_id": "a" * 64, "approved": approved})


@pytest.mark.asyncio
async def test_cancellation_after_submission_keeps_the_known_video_result():
    store = InMemoryStore()
    execution = trace(store)
    with pytest.raises(asyncio.CancelledError):
        async with execution.bind():
            await progress("video.submit", "提交视频")
            record_video_result(
                {"record_id": 7, "status": "PENDING", "storage_status": "NOT_READY"}
            )
            raise asyncio.CancelledError()
    snapshot = (await execution_history(store, "42", "thread"))[0]
    assert snapshot.steps[0].status == "succeeded"
    assert snapshot.video["record_id"] == 7
    assert snapshot.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"tool_status": "invalid"}, "failed"),
        ({"memory_status": "clarification_required"}, "waiting_input"),
        ({"email_status": "scheduled"}, "succeeded"),
        ({"email_status": "unknown"}, "unknown"),
    ],
)
async def test_business_outcomes_remain_distinct(metadata, expected):
    execution = trace(InMemoryStore())
    await execution.step("request.prepare", "处理请求")
    await execution.finish(AIMessage(content="结果", response_metadata=metadata))
    assert execution.snapshot.status == expected
    assert execution.snapshot.steps[0].status == expected
