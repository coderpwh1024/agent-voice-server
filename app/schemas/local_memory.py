"""Bounded device-owned memory references and stateless extraction proposals."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

LocalMemoryText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)
]
LayeredMemoryCategory = Literal["profile", "person", "project", "episode", "procedure"]
MemoryFactKey = Literal[
    "preferred_name", "response_style", "home_city", "preference", "status", "decision"
]
MemoryTopic = Literal["image", "video", "email", "project", "other"]
MemoryProcedure = Literal["image_review_first"]
IMAGE_REVIEW_FIRST_TEXT = "生成图片前先给我看画面方案，明确要求直接生成时再提交。"


class LocalMemoryReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    category: LayeredMemoryCategory
    text: LocalMemoryText
    procedure_key: MemoryProcedure | None = None
    event_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_procedure(self) -> "LocalMemoryReference":
        if (self.category == "procedure") != (self.procedure_key is not None):
            raise ValueError("Memory procedure does not match the category")
        if self.event_at is not None and self.category != "episode":
            raise ValueError("Only episodes have an event date")
        return self


class LocalMemoryContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(default=0, ge=0)
    epoch: int = Field(default=0, ge=0)
    total_count: int = Field(default=0, ge=0)
    memories: list[LocalMemoryReference] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_budget(self) -> "LocalMemoryContext":
        if sum(len(item.text) for item in self.memories) > 3000:
            raise ValueError("Local memory context exceeds 3000 characters")
        if len({item.id for item in self.memories}) != len(self.memories):
            raise ValueError("Local memory references must be distinct")
        return self


class LocalMemoryExtractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    text: str = Field(min_length=1, max_length=6000)
    occurred_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    utc_offset_minutes: int = Field(default=0, ge=-840, le=840)
    context: LocalMemoryContext = Field(default_factory=LocalMemoryContext)
    version: Literal[1, 2] = 1


class LocalMemoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "update", "delete", "clear"]
    category: LayeredMemoryCategory
    text: str = Field(default="", max_length=1000)
    evidence: str = Field(min_length=1, max_length=6000)
    target_id: UUID | None = None
    requires_confirmation: bool = True
    fact_key: MemoryFactKey | None = Field(default=None, exclude_if=lambda value: value is None)
    subject: str | None = Field(
        default=None, min_length=1, max_length=60, exclude_if=lambda value: value is None
    )
    topic: MemoryTopic | None = Field(default=None, exclude_if=lambda value: value is None)
    event_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    procedure_key: MemoryProcedure | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_action(self) -> "LocalMemoryProposal":
        if self.action in {"add", "update"} and not self.text.strip():
            raise ValueError("Memory text is required")
        if (self.action in {"update", "delete"}) != (self.target_id is not None):
            raise ValueError("Memory target does not match the operation")
        if self.category == "procedure":
            if self.action != "add" or self.procedure_key is None:
                raise ValueError("A procedure must be explicitly enabled or removed on the device")
        elif self.procedure_key is not None:
            raise ValueError("Memory procedure does not match the category")
        if self.fact_key is not None and self.category in {"episode", "procedure"}:
            raise ValueError("Only facts have stable fact keys")
        if self.event_at is not None and self.category != "episode":
            raise ValueError("Only episodes have an event date")
        return self


class LocalMemoryProposals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[LocalMemoryProposal] = Field(default_factory=list, max_length=6)


class LocalMemoryExtractionResult(LocalMemoryProposals):
    source_id: UUID
    revision: int
    epoch: int
    status: Literal["proposed", "no_change"]
