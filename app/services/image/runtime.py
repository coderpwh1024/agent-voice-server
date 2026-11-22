"""Share the lifespan-managed image service with conversational execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.image.service import ImageService

_service: ImageService | None = None


def set_image_service(service: ImageService | None) -> None:
    global _service
    _service = service


def get_image_service() -> ImageService | None:
    return _service
