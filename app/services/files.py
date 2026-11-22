"""Bounded Qiniu uploads and user-scoped PostgreSQL file metadata."""

import asyncio
import logging
import re
from typing import Protocol
from urllib.parse import quote, urlsplit
from uuid import uuid4

from psycopg import InterfaceError, OperationalError
from pydantic import HttpUrl
from qiniu import Auth, BucketManager, put_data
from qiniu.services.storage.uploaders import FormUploader

from app.core.settings import Settings
from app.core.uploads import UploadContent
from app.repositories.files import FileRepository
from app.schemas.files import UploadedFile

logger = logging.getLogger(__name__)


_MIME_TYPE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")


class FileTooLargeError(ValueError):
    pass


class FileUploadError(RuntimeError):
    pass


class FileStorage(Protocol):
    bucket_name: str

    async def upload(self, object_key: str, data: bytes, content_type: str) -> str: ...

    async def delete(self, object_key: str) -> None: ...


class QiniuFileStorage:
    def __init__(self, config: Settings, *, secure_transport: bool = False) -> None:
        if not config.QINIU_ACCESS_KEY or not config.QINIU_SECRET_KEY:
            raise ValueError("Qiniu access and secret keys are required for file uploads")
        if not config.QINIU_BUCKET_NAME or not config.QINIU_PUBLIC_BASE_URL:
            raise ValueError("Qiniu bucket and public base URL are required for file uploads")
        base_url = config.QINIU_PUBLIC_BASE_URL.rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in base_url)
        ):
            raise ValueError("QINIU_PUBLIC_BASE_URL must be an unsigned public HTTP(S) URL")
        self.auth = Auth(
            config.QINIU_ACCESS_KEY.get_secret_value(), config.QINIU_SECRET_KEY.get_secret_value()
        )
        self.bucket_name = config.QINIU_BUCKET_NAME
        self.base_url = str(HttpUrl(base_url)).rstrip("/")
        self.token_ttl_seconds = config.QINIU_UPLOAD_TOKEN_TTL_SECONDS
        self.max_bytes = config.FILE_UPLOAD_MAX_BYTES
        scheme = "https" if secure_transport else "http"
        self.manager = BucketManager(self.auth, preferred_scheme=scheme)
        self._secure_uploader = (
            FormUploader(self.bucket_name, preferred_scheme=scheme) if secure_transport else None
        )

    def _upload(self, object_key: str, data: bytes, content_type: str) -> None:
        token = self.auth.upload_token(
            self.bucket_name,
            object_key,
            self.token_ttl_seconds,
            policy={"insertOnly": 1, "fsizeLimit": self.max_bytes},
        )
        if self._secure_uploader is None:
            result, response = put_data(
                token, object_key, data, mime_type=content_type, check_crc=True
            )
        else:
            result, response = self._secure_uploader.upload(
                key=object_key, data=data, mime_type=content_type, up_token=token
            )
        if (
            response.status_code != 200
            or not isinstance(result, dict)
            or result.get("key") != object_key
            or not result.get("hash")
        ):
            raise FileUploadError("Qiniu did not confirm the file upload")

    async def upload(self, object_key: str, data: bytes, content_type: str) -> str:
        try:
            await asyncio.to_thread(self._upload, object_key, data, content_type)
        except Exception as exc:
            logger.warning("Qiniu file upload failed: error_type=%s", type(exc).__name__)
            raise FileUploadError("File upload failed") from None
        return f"{self.base_url}/{quote(object_key, safe='/')}"

    async def delete(self, object_key: str) -> None:
        try:
            _, response = await asyncio.to_thread(self.manager.delete, self.bucket_name, object_key)
            if response.status_code not in {200, 612}:
                logger.warning("Qiniu file cleanup unconfirmed: object_key=%s", object_key)
        except Exception as exc:
            logger.warning(
                "Qiniu file cleanup failed: object_key=%s error_type=%s",
                object_key,
                type(exc).__name__,
            )


class FileService:
    def __init__(self, repo: FileRepository, storage: FileStorage, max_bytes: int) -> None:
        self.repo = repo
        self.storage = storage
        self.max_bytes = max_bytes

    async def upload(self, user_id: int, file: UploadContent) -> UploadedFile:
        name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or len(name) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError(
                "File name must contain 1 to 255 characters without control characters"
            )
        content_type = (file.content_type or "application/octet-stream").split(";", 1)[0].lower()
        if len(content_type) > 127 or not _MIME_TYPE.fullmatch(content_type):
            raise ValueError("Invalid file content type")
        if file.size is not None and file.size > self.max_bytes:
            raise FileTooLargeError("File exceeds the upload size limit")
        data = await file.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise FileTooLargeError("File exceeds the upload size limit")
        if not data:
            raise ValueError("File is empty")
        # Finish the upload and metadata write before propagating cancellation of the request.
        task = asyncio.create_task(self._store(user_id, name, content_type, data))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception as exc:
                logger.warning("Canceled file upload failed: error_type=%s", type(exc).__name__)
            raise

    async def _store(self, user_id: int, name: str, content_type: str, data: bytes) -> UploadedFile:
        object_key = f"users/{user_id}/files/{uuid4().hex}"
        try:
            url = await self.storage.upload(object_key, data, content_type)
        except FileUploadError:
            await self.storage.delete(object_key)
            raise
        try:
            return await self.repo.create(
                user_id, url, self.storage.bucket_name, object_key, name, content_type, len(data)
            )
        except (OperationalError, InterfaceError):
            # A lost commit acknowledgment may still leave a valid row pointing at this object.
            logger.warning("File metadata write uncertain; retained object_key=%s", object_key)
            raise
        except Exception:
            await self.storage.delete(object_key)
            raise

    async def get(self, file_id: int, user_id: int) -> UploadedFile:
        file = await self.repo.get(file_id, user_id)
        if file is None:
            raise LookupError("File not found")
        return file
