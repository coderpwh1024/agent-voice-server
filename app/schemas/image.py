"""Public contracts for asynchronous Qwen image generation and editing."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, field_validator

ImageID = Annotated[int, Field(gt=0, le=9223372036854775807, strict=True)]
ImageModel = Literal[
    "qwen-image-3.0-pro", "qwen-image-3.0", "qwen-image-2.1-pro", "qwen-image-2.1-turbo"
]
ImageTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00]+$"),
]
ImageSearchField = Literal["title", "content"]
ImageLoopExit = Literal["ACCEPTED", "STOPPED"]
ImageLoopStatus = Literal["OPEN", "PROCESSING", "EXHAUSTED", "ACCEPTED", "STOPPED"]


class ImageStatus(StrEnum):
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    STORING = "STORING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    CANCELED = "CANCELED"

    @property
    def active(self) -> bool:
        return self in _ACTIVE_STATES


_ACTIVE_STATES = {
    ImageStatus.QUEUED,
    ImageStatus.SUBMITTING,
    ImageStatus.PENDING,
    ImageStatus.RUNNING,
    ImageStatus.STORING,
}


class ImageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=4000, pattern=r"^[^\x00]+$")
    title: ImageTitle | None = None
    source_file_ids: list[ImageID] = Field(default_factory=list, max_length=3)
    size: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,3}\*[1-9][0-9]{0,3}$")

    @field_validator("source_file_ids")
    @classmethod
    def validate_reference_ids(cls, ids: list[int]) -> list[int]:
        if len(set(ids)) != len(ids):
            raise ValueError("参考图片不能重复")
        return ids

    @field_validator("size")
    @classmethod
    def validate_image_size(cls, size: str | None) -> str | None:
        if size is not None:
            width, height = map(int, size.split("*"))
            if not 512**2 <= width * height <= 2048**2 or max(width, height) > 8 * min(
                width, height
            ):
                raise ValueError("图像大小和宽高比超出范围")
        return size


class ImageMetadataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: ImageTitle


class ImageTask(BaseModel):
    id: ImageID
    title: str
    prompt: str
    model: ImageModel
    status: ImageStatus
    source_image_id: ImageID | None
    source_file_ids: list[ImageID]
    size: str | None
    image_url: HttpUrl | None
    error_code: str | None
    create_time: datetime
    update_time: datetime
    loop_root_image_id: ImageID | None = Field(default=None, exclude_if=lambda value: value is None)
    loop_version: int | None = Field(default=None, ge=1, exclude_if=lambda value: value is None)


class ImageLoopClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ImageLoopExit


class ImageLoopState(BaseModel):
    root_image_id: ImageID
    used_edits: int = Field(ge=0)
    max_edits: int = Field(ge=1)
    status: ImageLoopStatus
    accepted_image_id: ImageID | None


class ImageLoop(ImageLoopState):
    versions: list[ImageTask]


class ImageLoopHistory(ImageLoop):
    root_image: ImageTask
    preview_image_id: ImageID
    preview_image_url: HttpUrl | None
    create_time: datetime
    update_time: datetime


class ImageLoopHistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, le=2147483647)
    page_size: int = Field(default=10, ge=1, le=100)


class ImageLoopHistoryPage(BaseModel):
    items: list[ImageLoopHistory]
    total: int
    page: int
    page_size: int
    total_pages: int


class ImageListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    page: int = Field(default=1, ge=1, le=2147483647)
    page_size: int = Field(default=20, ge=1, le=100)
    status: ImageStatus | None = None
    q: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[^\x00]+$")
    search_field: ImageSearchField | None = None


class ImageTaskPage(BaseModel):
    items: list[ImageTask]
    total: int
    page: int
    page_size: int
    total_pages: int
