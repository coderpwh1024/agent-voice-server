import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.agents import DEFAULT_AGENT, get_agent
from app.core import settings
from app.core.database import get_postgres_connection_string
from app.core.nacos import NacosIntegration
from app.core.persistence import initialize_database, initialize_store
from app.repositories.files import FileRepository
from app.repositories.users import UserRepository
from app.repositories.verification_codes import AsyncRedisProtocol, VerificationCodeStore
from app.repositories.video import VideoRepository
from app.repositories.voice import VoiceRepository
from app.services.email_auth import EmailAuthService, SMTPEmailSender
from app.services.files import FileService, QiniuFileStorage
from app.services.memory.mem0_service import mem0_lifespan
from app.services.skills.email import EmailSkillService, set_email_skill_service
from app.services.users import QiniuAvatarStorage, UserProfileService
from app.services.video.providers import HappyHorseClient, QiniuVideoStorage
from app.services.video.runtime import set_video_service
from app.services.video.service import VideoService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize the main agent, PostgreSQL persistence and application services."""
    nacos = NacosIntegration(settings)
    try:
        await nacos.load_remote_config()
        settings.require_amap_mcp_configuration()
        # Initialize both checkpointer (for short-term memory) and store (for long-term memory)
        async with initialize_database() as saver, initialize_store() as store:
            # Set up both components
            if hasattr(saver, "setup"):  # ignore: union-attr
                await saver.setup()
            # Initialize the persistent cross-conversation store.
            if hasattr(store, "setup"):  # ignore: union-attr
                await store.setup()

            if not settings.AUTH_SECRET and not settings.APP_TOKEN_SECRET:
                logger.warning(
                    "AUTH_SECRET and APP_TOKEN_SECRET are not configured — all API endpoints "
                    "are unauthenticated. Configure a bearer-token secret before deployment."
                )

            agent = get_agent(DEFAULT_AGENT)
            agent.checkpointer = saver
            agent.store = store
            logger.info("Agent initialized: %s", DEFAULT_AGENT)
            async with (
                mem0_lifespan(),
                email_auth_lifespan(app),
                email_skill_lifespan(app),
                voice_lifespan(app),
                video_lifespan(app),
                file_lifespan(app),
            ):
                await nacos.register_service()
                app.state.nacos = nacos
                try:
                    yield
                finally:
                    await nacos.shutdown()
    except Exception as e:
        logger.error("Error during service lifecycle initialization: %s", e)
        raise
    finally:
        await nacos.shutdown()


def _validate_email_auth_settings(config: Any) -> str:
    if config.APP_TOKEN_SECRET is None:
        raise ValueError("APP_TOKEN_SECRET is required for email authentication")
    token_secret = config.APP_TOKEN_SECRET.get_secret_value()
    if len(token_secret) < 32:
        raise ValueError("APP_TOKEN_SECRET must contain at least 32 characters")
    if not config.SMTP_HOST or not config.SMTP_FROM_EMAIL:
        raise ValueError("SMTP_HOST and SMTP_FROM_EMAIL are required for email authentication")
    if bool(config.SMTP_USERNAME) != bool(config.SMTP_PASSWORD):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be configured together")
    if config.SMTP_USE_TLS and config.SMTP_USE_SSL:
        raise ValueError("SMTP_USE_TLS and SMTP_USE_SSL cannot both be enabled")
    return token_secret


@asynccontextmanager
async def email_auth_lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.email_auth_service = None
    app.state.user_profile_service = None
    if not settings.EMAIL_AUTH_ENABLED:
        yield
        return
    token_secret = _validate_email_auth_settings(settings)
    users = UserRepository(get_postgres_connection_string(), settings.AUTH_SERVICE_ACCOUNT_ID)
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    codes = VerificationCodeStore(cast(AsyncRedisProtocol, redis))
    try:
        await users.open()
        await codes.ping()
        app.state.email_auth_service = EmailAuthService(
            users,
            codes,
            SMTPEmailSender(settings),
            token_secret,
        )
        app.state.user_profile_service = UserProfileService(
            users,
            QiniuAvatarStorage(settings),
            settings.QINIU_AVATAR_MAX_BYTES,
        )
        yield
    finally:
        app.state.email_auth_service = None
        app.state.user_profile_service = None
        await codes.close()
        await users.close()


@asynccontextmanager
async def email_skill_lifespan(app: FastAPI) -> AsyncIterator[None]:
    set_email_skill_service(None)
    if not settings.EMAIL_SKILL_ENABLED:
        yield
        return
    if not settings.NACOS_ENABLED or settings.email_skill is None:
        raise ValueError("Email skill must be loaded from Nacos before startup")
    auth = getattr(app.state, "email_auth_service", None)
    if auth is None:
        raise ValueError("EMAIL_AUTH_ENABLED is required for the email skill")
    if not settings.SMTP_USE_SSL and not settings.SMTP_USE_TLS:
        raise ValueError("The email skill requires an encrypted SMTP transport")
    if not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
        raise ValueError("The email skill requires SMTP credentials")
    service = EmailSkillService(auth.users, auth.sender, auth.codes.redis)
    set_email_skill_service(service)
    scheduler = asyncio.create_task(service.run_scheduler(), name="email-scheduler")
    logger.info("Email skill runtime started")
    try:
        yield
    finally:
        set_email_skill_service(None)
        scheduler.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler
        logger.info("Email skill runtime stopped")


@asynccontextmanager
async def file_lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.file_service = None
    if not settings.FILE_UPLOAD_ENABLED:
        yield
        return
    if not settings.APP_TOKEN_SECRET or len(settings.APP_TOKEN_SECRET.get_secret_value()) < 32:
        raise ValueError("File uploads require APP_TOKEN_SECRET with at least 32 characters")
    storage = QiniuFileStorage(settings)
    repo = FileRepository(get_postgres_connection_string())
    try:
        await repo.open()
        app.state.file_service = FileService(repo, storage, settings.FILE_UPLOAD_MAX_BYTES)
        yield
    finally:
        app.state.file_service = None
        await repo.close()


@asynccontextmanager
async def voice_lifespan(app: FastAPI):
    app.state.voice_repository = None
    app.state.voice_connections = {}
    if not settings.VOICE_ENABLED and not settings.APP_TOKEN_SECRET:
        yield
        return
    if settings.VOICE_ENABLED and not settings.AUTH_SECRET:
        raise ValueError("AUTH_SECRET is required for realtime voice")
    if settings.VOICE_ENABLED and not settings.APP_TOKEN_SECRET:
        raise ValueError("APP_TOKEN_SECRET is required for realtime voice")
    if settings.APP_TOKEN_SECRET and len(settings.APP_TOKEN_SECRET.get_secret_value()) < 32:
        raise ValueError("APP_TOKEN_SECRET must contain at least 32 characters")
    if settings.VOICE_ENABLED and not settings.DASHSCOPE_API_KEY:
        raise ValueError("DASHSCOPE_API_KEY is required for realtime voice")
    settings.require_voice_configuration()
    repo = VoiceRepository(get_postgres_connection_string())
    try:
        await repo.open()
        app.state.voice_repository = repo
        yield
    finally:
        connections = list(app.state.voice_connections.values())
        await asyncio.gather(*(c.stop() for c in connections), return_exceptions=True)
        tasks = [getattr(c, "task", None) for c in connections]
        await asyncio.gather(*(t for t in tasks if t), return_exceptions=True)
        app.state.voice_repository = None
        await repo.close()


@asynccontextmanager
async def video_lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.video_service = None
    set_video_service(None)
    if not settings.VIDEO_ENABLED:
        yield
        return
    settings.require_video_configuration()
    repo = VideoRepository(get_postgres_connection_string())
    worker: asyncio.Task[None] | None = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10), follow_redirects=False
    ) as client:
        try:
            await repo.open()
            service = VideoService(
                repo,
                HappyHorseClient(client, settings),
                QiniuVideoStorage(settings),
                settings.AUTH_SERVICE_ACCOUNT_ID,
            )
            app.state.video_service = service
            set_video_service(service)
            worker = asyncio.create_task(service.run(), name="video-sync")
            yield
        finally:
            app.state.video_service = None
            set_video_service(None)
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            await repo.close()
