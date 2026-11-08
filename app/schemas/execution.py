"""Public, transport-independent execution progress and confirmation contracts."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

ExecutionStatus = Literal[
    "running",
    "succeeded",
    "warning",
    "failed",
    "cancelled",
    "waiting_input",
    "waiting_confirmation",
    "unknown",
]


class MemoryConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_id: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    approved: StrictBool


class ExecutionConfirmation(BaseModel):
    confirmation_id: str
    label: str
    expires_at: datetime


class ExecutionStep(BaseModel):
    step_id: str
    stage: str
    label: str
    status: ExecutionStatus = "running"
    detail: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int | None = Field(default=None, ge=0)


class ExecutionSnapshot(BaseModel):
    type: Literal["execution.progress"] = "execution.progress"
    version: Literal[1] = 1
    run_id: str
    response_id: str | None = None
    message_id: str
    input_message_id: str | None = None
    sequence: int = Field(default=0, ge=0)
    status: ExecutionStatus = "running"
    steps: list[ExecutionStep] = Field(default_factory=list, max_length=50)
    confirmation: ExecutionConfirmation | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    text: str = ""
    video: dict[str, Any] | None = None


class ExecutionList(BaseModel):
    executions: list[ExecutionSnapshot]
