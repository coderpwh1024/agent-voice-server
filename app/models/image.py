"""Image queue persistence records and domain conflicts."""

from datetime import datetime

from pydantic import BaseModel

from app.schemas.image import ImageStatus, ImageTask


class ImageInputError(ValueError):
    pass


class ImageConflictError(RuntimeError):
    pass


class ImageBusyError(ImageConflictError):
    pass


class ImageEditLimitError(ImageConflictError):
    pass


class ImageClaim(ImageTask):
    previous_status: ImageStatus
    provider_task_id: str | None
    provider_image_url: str | None
    provider_deadline: datetime
    source_urls: list[str]
    lease_version: int
    failure_count: int


class ImageProgress(BaseModel):
    status: ImageStatus
    provider_task_id: str | None = None
    provider_request_id: str | None = None
    provider_image_url: str | None = None
    image_url: str | None = None
    qiniu_object_key: str | None = None
    error_code: str | None = None
    failure_count: int = 0
    delay_seconds: int = 0
