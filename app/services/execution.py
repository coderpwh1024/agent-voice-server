"""Publish real execution boundaries and retain recoverable PostgreSQL Store snapshots."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.store.base import BaseStore
from langgraph.types import StreamWriter

from app.schemas.events import CustomData
from app.schemas.execution import ExecutionSnapshot, ExecutionStatus, ExecutionStep
from app.schemas.memory_commands import PendingMemoryCommand
from app.services.messages import convert_message_content_to_string

_current: ContextVar["ExecutionTrace | None"] = ContextVar("execution_trace", default=None)


def confirmation_id(pending: PendingMemoryCommand) -> str:
    payload = json.dumps(pending.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def execution_namespace(user_id: str, thread_id: str) -> tuple[str, ...]:
    return ("execution", user_id, thread_id)


async def execution_history(
    store: BaseStore | None, user_id: str, thread_id: str
) -> list[ExecutionSnapshot]:
    if store is None:
        return []
    rows = await store.asearch(execution_namespace(user_id, thread_id), limit=100)
    snapshots = [ExecutionSnapshot.model_validate(row.value) for row in rows]
    for snapshot in snapshots:
        if snapshot.status == "running" and snapshot.updated_at < datetime.now(UTC) - timedelta(
            minutes=5
        ):
            snapshot.status = "unknown"
            for step in snapshot.steps:
                if step.status == "running":
                    step.status = "unknown"
                    step.detail = "执行状态长时间未更新，请核实业务结果后再决定是否重试"
        if snapshot.confirmation and snapshot.confirmation.expires_at <= datetime.now(UTC):
            snapshot.confirmation = None
            if snapshot.status == "waiting_confirmation":
                snapshot.status = "cancelled"
                for step in snapshot.steps:
                    if step.status == "waiting_confirmation":
                        step.status = "cancelled"
                        step.detail = "确认已过期，请重新发起操作"
    return sorted(snapshots, key=lambda item: item.updated_at)


class ExecutionTrace:
    def __init__(
        self,
        config: RunnableConfig,
        store: BaseStore | None,
        writer: StreamWriter,
        input_message_id: str | None,
    ) -> None:
        self.config = config
        metadata = config.get("metadata", {})
        self.snapshot = ExecutionSnapshot(
            run_id=str(metadata.get("execution_run_id") or uuid4()),
            response_id=metadata.get("execution_response_id"),
            message_id=str(uuid4()),
            input_message_id=input_message_id,
        )
        values = config.get("configurable", {})
        self.namespace = execution_namespace(str(values["user_id"]), str(values["thread_id"]))
        self.store = store
        self.writer = writer
        self.started: dict[str, float] = {}

    @asynccontextmanager
    async def bind(self) -> AsyncIterator[None]:
        token = _current.set(self)
        try:
            yield
        except asyncio.CancelledError:
            await self.abort(True)
            raise
        except Exception:
            await self.abort(False)
            raise
        finally:
            _current.reset(token)

    async def publish(self) -> None:
        self.snapshot.sequence += 1
        self.snapshot.updated_at = datetime.now(UTC)
        data = self.snapshot.model_dump(mode="json")
        if self.store is not None:
            await self.store.aput(self.namespace, self.snapshot.run_id, data, index=False)
        CustomData(data=data).dispatch(self.writer)
        if self.config.get("callbacks") is not None:
            await adispatch_custom_event("execution.progress", data, config=self.config)

    async def step(
        self, stage: str, label: str, status: ExecutionStatus = "running", detail: str | None = None
    ) -> None:
        step = next((item for item in reversed(self.snapshot.steps) if item.stage == stage), None)
        if step is None or (status == "running" and step.status != "running"):
            step = ExecutionStep(step_id=str(uuid4()), stage=stage, label=label)
            self.snapshot.steps.append(step)
            self.started[step.step_id] = monotonic()
        step.status = status
        step.detail = detail
        if status != "running":
            step.duration_ms = int((monotonic() - self.started[step.step_id]) * 1000)
        await self.publish()

    async def finish(self, response: AIMessage) -> None:
        metadata = response.response_metadata
        outcome: ExecutionStatus = "succeeded"
        business_status = (
            metadata.get("video_status")
            or metadata.get("email_status")
            or metadata.get("memory_status")
            or metadata.get("tool_status")
        )
        if business_status in {"confirmation_required"}:
            outcome = "waiting_confirmation"
        elif business_status in {
            "draft",
            "clarification",
            "clarification_required",
            "selection_required",
            "invalid_time",
            "missing",
        }:
            outcome = "waiting_input"
        elif business_status in {"unknown", "operation_failed", "submission_unknown", "busy"}:
            outcome = "unknown"
        elif business_status == "cancelled":
            outcome = "cancelled"
        elif business_status in {
            "invalid",
            "timeout",
            "unavailable",
            "rejected",
            "forbidden",
            "unauthenticated",
            "conflict",
            "not_found",
            "limited",
            "unsupported",
            "no_pending",
            "expired",
        }:
            outcome = "failed"
        if business_status == "unavailable" and any(
            step.stage == "video.submit" for step in self.snapshot.steps
        ):
            outcome = "unknown"
        if business_status == "disabled" and any(
            step.stage == "memory.command" for step in self.snapshot.steps
        ):
            outcome = "warning"
        video = metadata.get("video")
        if isinstance(video, dict):
            if video.get("status") in {"SUBMISSION_UNKNOWN", "UNKNOWN"}:
                outcome = "unknown"
            elif video.get("status") in {"FAILED", "CANCELED"}:
                outcome = "failed"
        for step in self.snapshot.steps:
            if step.status == "running":
                step.status = outcome
                step.duration_ms = int((monotonic() - self.started[step.step_id]) * 1000)
        self.snapshot.status = outcome
        self.snapshot.text = convert_message_content_to_string(response.content)
        self.snapshot.video = metadata.get("video")
        if response.id:
            self.snapshot.message_id = response.id
        else:
            response.id = self.snapshot.message_id
        await self.publish()
        metadata["execution"] = self.snapshot.model_dump(mode="json")

    async def abort(self, cancelled: bool) -> None:
        self.snapshot.status = "cancelled" if cancelled else "failed"
        for step in self.snapshot.steps:
            if step.status == "running":
                step.status = (
                    "unknown"
                    if step.stage
                    in {"email.submit", "video.submit", "memory.command", "memory.write"}
                    else self.snapshot.status
                )
                step.duration_ms = int((monotonic() - self.started[step.step_id]) * 1000)
        await asyncio.shield(self.publish())


async def progress(
    stage: str, label: str, status: ExecutionStatus = "running", detail: str | None = None
) -> None:
    trace = _current.get()
    if trace is not None:
        await trace.step(stage, label, status, detail)


def record_video_result(value: dict[str, object]) -> None:
    trace = _current.get()
    if trace is None:
        return
    trace.snapshot.video = value
    status: ExecutionStatus = "succeeded"
    if value.get("status") in {"SUBMISSION_UNKNOWN", "UNKNOWN"}:
        status = "unknown"
    elif value.get("status") in {"FAILED", "CANCELED"}:
        status = "failed"
    for step in trace.snapshot.steps:
        if step.stage == "video.submit" and step.status == "running":
            step.status = status
            step.duration_ms = int((monotonic() - trace.started[step.step_id]) * 1000)
