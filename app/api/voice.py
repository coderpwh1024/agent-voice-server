"""Mobile realtime voice endpoints, identity checks and service lifecycle."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, WebSocket

from app.agents import DEFAULT_AGENT, get_all_agent_info
from app.api.dependencies import get_agent, principal, repository
from app.api.voice_connection import VoiceConnection
from app.core import settings
from app.core.agent_ids import uses_streaming_chat
from app.core.exceptions import ServiceError
from app.core.speech import voice_options
from app.repositories.voice import VoiceRepository
from app.schemas import ApiResponse, api_success
from app.schemas.voice import (
    VoiceSession,
    VoiceSessionInput,
    client_event_adapter,
    normalize_wake_word,
)
from app.services.agent_runner import thread_guard
from app.services.auth import authenticate, authorize_thread, bind_user
from app.services.messages import ensure_model_available
from app.services.voice.providers.alibaba_realtime import AlibabaRealtime

router = APIRouter(prefix="/voice", tags=["voice"])


def require_voice(request: Request) -> VoiceRepository:
    repo = repository(request)
    if not settings.VOICE_ENABLED or repo is None:
        raise HTTPException(503, "Realtime voice is disabled")
    return repo


async def owned_session(repo: VoiceRepository, session_id: UUID, identity) -> VoiceSession:
    session = await repo.get_session(str(session_id))
    if session is None:
        raise HTTPException(404, "Voice session not found")
    if not identity.admin and session.user_id != identity.user_id:
        raise HTTPException(403, "Voice session belongs to another user")
    return session


@router.get("/capabilities", response_model=ApiResponse[dict[str, Any]])
async def capabilities(request: Request) -> ApiResponse[dict[str, Any]]:
    principal(request)
    options = voice_options(settings) if settings.VOICE_ENABLED else []
    voice_ids = [voice.id for voice in options]
    return api_success(
        {
            "enabled": settings.VOICE_ENABLED,
            "default_agent": DEFAULT_AGENT,
            "protocol_version": 1,
            "local_memory": {
                "enabled": settings.LOCAL_MEMORY_ENABLED,
                "version": 1,
                "max_context_chars": 3000,
                "max_references": 8,
            },
            "input_format": "pcm16_16000_mono",
            "output_format": "pcm16_24000_mono",
            "audio_header_bytes": 45,
            "max_audio_payload_bytes": 6400,
            "voices": voice_ids,
            "default_voice": voice_ids[0] if voice_ids else None,
            "voice_options": [voice.model_dump() for voice in options],
            "stt_model": settings.VOICE_REALTIME_STT_MODEL,
            "tts_model": settings.VOICE_REALTIME_TTS_MODEL,
            "voice_catalog_model": settings.VOICE_REALTIME_TTS_MODEL,
            "turn_detection_modes": ["server_vad", "manual"],
            "input_modes": ["conversation", "text", "transcription"],
            "session_seconds": settings.VOICE_SESSION_SECONDS,
            "wake_word": {
                "enabled": settings.VOICE_WAKE_ENABLED,
                "keyword": settings.VOICE_WAKE_WORD,
                "confirm_with_asr": settings.VOICE_WAKE_CONFIRM_WITH_ASR,
                "pre_roll_ms": settings.VOICE_WAKE_PRE_ROLL_MS,
                "kws_score": settings.VOICE_WAKE_KWS_SCORE,
                "kws_threshold": settings.VOICE_WAKE_KWS_THRESHOLD,
            },
            "client_vad": {
                "enabled": True,
                "rms_dbfs": settings.VOICE_CLIENT_VAD_RMS_DBFS,
                "speech_frames": settings.VOICE_CLIENT_VAD_FRAMES,
            },
            "audio_metrics_seconds": settings.VOICE_AUDIO_METRICS_SECONDS,
            "agents": [
                {
                    "id": a.key,
                    "description": a.description,
                    "speech_mode": "streaming" if uses_streaming_chat(a.key) else "final_message",
                    "cancel_execution": uses_streaming_chat(a.key),
                }
                for a in get_all_agent_info()
            ],
        }
    )


@router.get("/protocol", response_model=ApiResponse[dict[str, Any]])
async def protocol(request: Request) -> ApiResponse[dict[str, Any]]:
    principal(request)
    return api_success(client_event_adapter.json_schema())


@router.post("/sessions", response_model=ApiResponse[VoiceSession])
async def create_session(body: VoiceSessionInput, request: Request) -> ApiResponse[VoiceSession]:
    identity = principal(request)
    repo = require_voice(request)
    user_id = bind_user(identity, body.user_id)
    if body.memory_mode == "local" and not settings.LOCAL_MEMORY_ENABLED:
        raise HTTPException(503, "Device memory is disabled")
    try:
        agent = get_agent(body.agent_id)
    except (HTTPException, KeyError) as exc:
        if isinstance(exc, HTTPException) and exc.status_code != 404:
            raise
        raise HTTPException(404, "Agent not found") from None
    options = voice_options(settings)
    voice_ids = [voice.id for voice in options]
    if not voice_ids:
        raise HTTPException(503, "No realtime voices are configured")
    voice = body.voice or voice_ids[0]
    if voice not in voice_ids:
        raise HTTPException(422, "Unsupported voice")
    selected = next(option for option in options if option.id == voice)
    if body.language != "auto" and body.language not in selected.languages:
        raise HTTPException(422, "The selected voice does not support this language")
    if body.model is not None:
        ensure_model_available(body.model)
    if body.activation == "wake_word":
        if not settings.VOICE_WAKE_ENABLED:
            raise HTTPException(422, "Wake-word activation is disabled")
        if normalize_wake_word(body.wake_word or "") != settings.VOICE_WAKE_WORD:
            raise HTTPException(422, "Unsupported wake word")
        body.wake_word = settings.VOICE_WAKE_WORD
        maximum_pre_roll = settings.VOICE_WAKE_PRE_ROLL_MS * 16
        if body.pre_roll_samples > maximum_pre_roll:
            raise HTTPException(422, "Wake pre-roll is too long")
    thread_id = body.thread_id or str(uuid4())
    async with repo.guard(f"voice-user:{user_id}"), thread_guard(repo, thread_id):
        if await repo.active_sessions(user_id) >= 2:
            raise HTTPException(429, "At most two unexpired voice sessions per user")
        await authorize_thread(
            repo, agent, thread_id, body.agent_id, identity, user_id, create=True
        )
        session = VoiceSession(
            **body.model_dump(exclude={"thread_id", "user_id", "voice"}),
            voice=voice,
            thread_id=thread_id,
            user_id=user_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.VOICE_SESSION_SECONDS),
            wake_status=(
                "pending"
                if body.activation == "wake_word" and settings.VOICE_WAKE_CONFIRM_WITH_ASR
                else "accepted"
                if body.activation == "wake_word"
                else "not_required"
            ),
        )
        await repo.save_session(session)
    return api_success(session)


@router.get("/sessions/{session_id}", response_model=ApiResponse[VoiceSession])
async def get_session(session_id: UUID, request: Request) -> ApiResponse[VoiceSession]:
    session = await owned_session(require_voice(request), session_id, principal(request))
    return api_success(session)


@router.get("/sessions/{session_id}/turns", response_model=ApiResponse[dict[str, Any]])
async def get_turns(session_id: UUID, request: Request) -> ApiResponse[dict[str, Any]]:
    repo = require_voice(request)
    session = await owned_session(repo, session_id, principal(request))
    return api_success(
        {
            "turns": [
                t for t in await repo.turns(session.thread_id) if t.session_id == session.session_id
            ]
        }
    )


@router.delete("/sessions/{session_id}", response_model=ApiResponse[dict[str, str]])
async def close_session(session_id: UUID, request: Request) -> ApiResponse[dict[str, str]]:
    repo = require_voice(request)
    session = await owned_session(repo, session_id, principal(request))
    connection = request.app.state.voice_connections.get(str(session_id))
    if connection:
        await connection.stop()
        return api_success({"status": "closing"})
    async with repo.guard(f"voice-session:{session_id}"):
        session.status = "closed"
        await repo.save_session(session)
    return api_success({"status": "closed"})


@router.websocket("/sessions/{session_id}/ws")
async def voice_websocket(websocket: WebSocket, session_id: UUID):
    connection = None
    registered = False
    try:
        identity = authenticate(websocket.headers.get("authorization"))
        repo = getattr(websocket.app.state, "voice_repository", None)
        if not settings.VOICE_ENABLED or repo is None:
            raise HTTPException(503, "Realtime voice is disabled")
        session = await owned_session(repo, session_id, identity)
        if session.status == "closed" or session.expires_at <= datetime.now(UTC):
            raise HTTPException(410, "Voice session expired or closed")
        if session.voice not in {option.id for option in voice_options(settings)}:
            raise HTTPException(409, "Speech configuration changed; create a new voice session")
        connections = websocket.app.state.voice_connections
        if len(connections) >= settings.VOICE_MAX_SESSIONS:
            raise HTTPException(429, "Voice capacity reached")
        async with repo.guard(f"voice-session:{session_id}"):
            session = await owned_session(repo, session_id, identity)
            if session.status == "closed":
                raise HTTPException(410, "Voice session is closed")
            connection = VoiceConnection(
                websocket,
                session,
                repo,
                get_agent(session.agent_id),
                AlibabaRealtime(settings),
                settings,
                identity.expires_at,
                authenticated_user_id=identity.user_id,
            )
            connection.task = asyncio.current_task()
            connections[str(session_id)] = connection
            registered = True
            await websocket.accept(subprotocol=None)
            await connection.run()
    except (HTTPException, ServiceError) as exc:
        await websocket.close(
            code={401: 4401, 403: 4403, 404: 4404, 409: 4409, 410: 4410, 429: 4429}.get(
                exc.status_code, 1013
            )
        )
    finally:
        if registered:
            websocket.app.state.voice_connections.pop(str(session_id), None)
