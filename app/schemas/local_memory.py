"""Bounded device-owned memory references and stateless extraction proposals."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

LocalMemoryText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)
]
MemoryCategory = Literal["profile", "person", "project", "episode"]


class LocalMemoryReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    category: MemoryCategory
    text: LocalMemoryText


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


class LocalMemoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "update", "delete", "clear"]
    category: MemoryCategory
    text: str = Field(default="", max_length=1000)
    evidence: str = Field(min_length=1, max_length=6000)
    target_id: UUID | None = None
    requires_confirmation: bool = True

    @model_validator(mode="after")
    def validate_action(self) -> "LocalMemoryProposal":
        if self.action in {"add", "update"} and not self.text.strip():
            raise ValueError("Memory text is required")
        if (self.action in {"update", "delete"}) != (self.target_id is not None):
            raise ValueError("Memory target does not match the operation")
        return self


class LocalMemoryProposals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[LocalMemoryProposal] = Field(default_factory=list, max_length=6)


class LocalMemoryExtractionResult(LocalMemoryProposals):
    source_id: UUID
    revision: int
    epoch: int
    status: Literal["proposed", "no_change"]
