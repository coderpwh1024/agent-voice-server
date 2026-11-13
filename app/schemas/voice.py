"""Versioned mobile voice contract and framed PCM transport."""

import re
import struct
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter, model_validator
from pydantic_core import PydanticCustomError

from app.schemas.llm import AllModelEnum
from app.schemas.local_memory import LocalMemoryContext
from app.schemas.location import DeviceLocation

PROTOCOL_VERSION = 1


AUDIO_HEADER = struct.Struct("!4sB16s16sII")


MAX_AUDIO_BYTES = 6400


ZERO_UUID = UUID(int=0)


def normalize_wake_word(keyword: str) -> str:
    normalized = re.sub(r"[\s，,。！？!?、；;：:．.…]+", "", keyword)
    return "小美小美" if normalized == "小美" else normalized


class VoiceOption(BaseModel):
    """Configured TTS voice metadata exposed to voice clients."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)
    languages: list[Literal["zh", "en"]] = Field(default=["zh", "en"], min_length=1, max_length=2)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_voice_id(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"id": value, "name": value}
        return value


class VoiceSessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(default="chatbot", min_length=1, max_length=100)
    memory_mode: Literal["mem0", "local"] = "mem0"
    model: AllModelEnum | None = None
    thread_id: str | None = Field(default=None, min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    voice: str | None = Field(default=None, min_length=1, max_length=100)
    language: Literal["zh", "en", "auto"] = "zh"
    turn_detection: Literal["server_vad", "manual"] = "server_vad"
    input_mode: Literal["conversation", "text", "transcription"] = "conversation"
    activation: Literal["tap", "wake_word"] = "tap"
    wake_word: str | None = Field(default=None, min_length=1, max_length=40)
    wake_engine: str | None = Field(default=None, min_length=1, max_length=100)
    pre_roll_samples: int = Field(default=0, ge=0, le=96000)

    @model_validator(mode="after")
    def validate_activation(self) -> "VoiceSessionInput":
        if self.input_mode != "conversation" and self.activation != "tap":
            raise PydanticCustomError(
                "input_mode_activation", "text and transcription modes require tap activation"
            )
        if self.input_mode == "transcription" and self.turn_detection != "manual":
            raise PydanticCustomError(
                "input_mode_turn_detection", "transcription mode requires manual turn detection"
            )
        if self.activation == "wake_word" and not self.wake_word:
            raise ValueError("wake_word is required for wake-word activation")
        if self.activation == "tap" and (
            self.wake_word or self.wake_engine or self.pre_roll_samples
        ):
            raise ValueError("wake metadata requires wake-word activation")
        return self


class AudioQualitySummary(BaseModel):
    reports: int = 0
    frames: int = 0
    clipped_samples: int = 0
    rms_dbfs_sum: float = 0
    peak_dbfs: float = -120
    aec_enabled: bool = False
    noise_suppression_enabled: bool = False

    @property
    def average_rms_dbfs(self) -> float | None:
        return self.rms_dbfs_sum / self.reports if self.reports else None


class VoiceSession(BaseModel):
    agent_id: str = "chatbot"
    memory_mode: Literal["mem0", "local"] = "mem0"
    model: AllModelEnum | None = None
    voice: str
    language: Literal["zh", "en", "auto"] = "zh"
    turn_detection: Literal["server_vad", "manual"] = "server_vad"
    input_mode: Literal["conversation", "text", "transcription"] = "conversation"
    activation: Literal["tap", "wake_word"] = "tap"
    wake_word: str | None = None
    wake_engine: str | None = None
    pre_roll_samples: int = 0
    wake_status: Literal["not_required", "pending", "accepted", "rejected"] = "not_required"
    audio_quality: AudioQualitySummary = Field(default_factory=AudioQualitySummary)
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    thread_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    status: Literal["ready", "connected", "disconnected", "closed"] = "ready"
    connection_id: str | None = None
    protocol_version: Literal[1] = 1


class AudioSegment(BaseModel):
    index: int
    text: str
    samples: int = 0
    played_samples: int = 0
    complete: bool = False


class VoiceTurn(BaseModel):
    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    response_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    thread_id: str
    input_id: str
    input_text: str
    run_id: str | None = None
    status: Literal["queued", "running", "completed", "cancelled", "failed", "waiting_approval"] = (
        "queued"
    )
    execution_status: Literal[
        "queued", "running", "completed", "cancelled", "failed", "waiting_approval"
    ] = "queued"
    audio_status: Literal["queued", "running", "completed", "cancelled", "failed"] = "queued"
    generated_text: str = ""
    segments: list[AudioSegment] = Field(default_factory=list)
    interrupted: bool = False
    error_code: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_audio_status(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "audio_status" in value:
            return value
        migrated = dict(value)
        if migrated.get("error_code") in {"tts_failed", "tts_backpressure"}:
            migrated["audio_status"] = "failed"
        elif migrated.get("status") == "cancelled" or migrated.get("interrupted"):
            migrated["audio_status"] = "cancelled"
        elif migrated.get("status") in {"completed", "failed", "waiting_approval"}:
            migrated["audio_status"] = "completed"
        else:
            migrated["audio_status"] = migrated.get("status", "queued")
        return migrated


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=128)


class Configure(Event):
    type: Literal["session.configure"]
    protocol_version: Literal[1] = 1
    input_format: Literal["pcm16_16000_mono"] = "pcm16_16000_mono"
    output_format: Literal["pcm16_24000_mono"] = "pcm16_24000_mono"


class TextInput(Event):
    type: Literal["input.text"]
    text: str = Field(min_length=1, max_length=6000)
    device_location: DeviceLocation | None = None


class LocationPayload(Event):
    status: Literal["ok", "denied", "unavailable"]
    device_location: DeviceLocation | None = None

    @model_validator(mode="after")
    def require_location_only_on_success(self) -> "LocationPayload":
        if (self.status == "ok") != (self.device_location is not None):
            raise ValueError("Location is required only for successful results")
        return self


class LocationUpdate(LocationPayload):
    type: Literal["location.update"]


class LocationResult(LocationPayload):
    type: Literal["location.result"]
    request_id: str = Field(min_length=1, max_length=128)


class CommitInput(Event):
    type: Literal["input.commit"]


class CancelResponse(Event):
    type: Literal["response.cancel"]
    response_id: UUID


class SpeechHint(Event):
    type: Literal["input.speech_hint"]


class AudioMetrics(Event):
    type: Literal["audio.metrics"]
    frames: int = Field(ge=1, le=10000)
    rms_dbfs: float = Field(ge=-120, le=0)
    peak_dbfs: float = Field(ge=-120, le=0)
    clipped_samples: int = Field(ge=0, le=10_000_000)
    aec_enabled: bool
    noise_suppression_enabled: bool
    mode: Literal["standby", "conversation"]


class PlaybackProgress(Event):
    type: Literal["playback.progress"]
    response_id: UUID
    segment_index: int = Field(ge=1)
    played_samples: int = Field(ge=0)


class PlaybackFinished(Event):
    type: Literal["playback.finished"]
    response_id: UUID


class ApprovalInput(Event):
    type: Literal["approval.submit"]
    interrupt_id: str = Field(min_length=1, max_length=128)
    value: Any


class MemoryConfirmationInput(Event):
    type: Literal["memory.confirm"]
    confirmation_id: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    approved: StrictBool


class LocalMemoryContextInput(Event):
    type: Literal["memory.context"]
    response_id: UUID
    context: LocalMemoryContext


class Ping(Event):
    type: Literal["ping"]


class CloseSession(Event):
    type: Literal["session.close"]


ClientEvent = Annotated[
    Configure
    | TextInput
    | LocationUpdate
    | LocationResult
    | CommitInput
    | CancelResponse
    | SpeechHint
    | AudioMetrics
    | PlaybackProgress
    | PlaybackFinished
    | ApprovalInput
    | MemoryConfirmationInput
    | LocalMemoryContextInput
    | Ping
    | CloseSession,
    Field(discriminator="type"),
]


client_event_adapter: TypeAdapter[ClientEvent] = TypeAdapter(ClientEvent)


def encode_audio(
    pcm: bytes,
    connection_id: str,
    response_id: str,
    segment_index: int,
    sequence: int,
    *,
    kind: int = 2,
) -> bytes:
    if not pcm or len(pcm) > MAX_AUDIO_BYTES or len(pcm) % 2:
        raise ValueError("Invalid PCM frame size")
    return (
        AUDIO_HEADER.pack(
            b"VCE1",
            kind,
            UUID(connection_id).bytes,
            UUID(response_id).bytes,
            segment_index,
            sequence,
        )
        + pcm
    )


def decode_audio(data: bytes) -> tuple[int, str, str, int, int, bytes]:
    if not AUDIO_HEADER.size < len(data) <= AUDIO_HEADER.size + MAX_AUDIO_BYTES:
        raise ValueError("Invalid audio frame size")
    magic, kind, connection, response, segment, sequence = AUDIO_HEADER.unpack_from(data)
    pcm = data[AUDIO_HEADER.size :]
    if magic != b"VCE1" or kind not in (1, 2) or len(pcm) % 2:
        raise ValueError("Invalid audio frame")
    return kind, str(UUID(bytes=connection)), str(UUID(bytes=response)), segment, sequence, pcm
