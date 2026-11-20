"""Translate Bailian task-based speech streams to the mobile voice contract."""

import asyncio
import json
import re
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, Literal
from uuid import uuid4

from websockets.asyncio.client import ClientConnection

from app.core.settings import Settings
from app.schemas.voice import VoiceSession
from app.services.voice.providers.errors import SpeechProviderError


class InferenceStream:
    def __init__(
        self,
        ws: ClientConnection,
        session: VoiceSession,
        config: Settings,
        role: Literal["stt", "tts"],
    ) -> None:
        self.ws = ws
        self.session = session
        self.config = config
        self.role = role
        self.task_id = ""
        self.ready = asyncio.Event()
        self.pending: deque[dict[str, Any]] = deque()
        self.sentences: dict[str, str] = {}
        self.started: set[str] = set()
        self.finished: set[str] = set()
        self.audio_samples = 0
        self.task_audio_offset_ms = 0

    async def send(self, action: str, payload: dict[str, Any]) -> None:
        async with asyncio.timeout(self.config.VOICE_UPSTREAM_TIMEOUT):
            await self.ws.send(
                json.dumps(
                    {
                        "header": {
                            "action": action,
                            "task_id": self.task_id,
                            "streaming": "duplex",
                        },
                        "payload": payload,
                    }
                )
            )

    async def read(self, *, timeout: bool = True) -> dict[str, Any] | bytes:
        async with asyncio.timeout(self.config.VOICE_UPSTREAM_TIMEOUT if timeout else None):
            raw = await self.ws.recv()
        if isinstance(raw, bytes):
            if self.role != "tts" or len(raw) % 2:
                raise SpeechProviderError("Invalid inference PCM frame")
            return raw
        try:
            event = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise SpeechProviderError("Invalid inference event") from exc
        if not isinstance(event, dict) or not isinstance(event.get("header"), dict):
            raise SpeechProviderError("Invalid inference event header")
        header = event["header"]
        if header.get("task_id") != self.task_id:
            raise SpeechProviderError("Inference event belongs to a different task")
        if header.get("event") == "task-failed":
            code = str(header.get("error_code", "unknown"))
            code = code if re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", code) else "unknown"
            raise SpeechProviderError(
                f"Speech inference task failed: {code}",
                code=code,
                retryable=code not in {"InvalidParameter", "InvalidApiKey", "AccessDenied"},
            )
        return event

    async def start(self) -> None:
        self.ready.clear()
        self.task_id = str(uuid4())
        self.sentences.clear()
        self.started.clear()
        self.finished.clear()
        self.task_audio_offset_ms = self.audio_samples // 16
        model = (
            self.config.VOICE_REALTIME_STT_MODEL
            if self.role == "stt"
            else self.config.VOICE_REALTIME_TTS_MODEL
        )
        parameters: dict[str, Any] = {
            "format": "pcm",
            "sample_rate": 16000 if self.role == "stt" else 24000,
        }
        if self.role == "stt":
            parameters["max_sentence_silence"] = self.config.VOICE_VAD_SILENCE_MS
            if model.startswith("qwen-audio-"):
                parameters["speech_noise_threshold"] = self.config.VOICE_VAD_THRESHOLD
            if model.startswith("qwen-audio-3.1-"):
                parameters["vad_model"] = self.config.VOICE_ASR_VAD_MODEL
            if model.startswith("qwen-audio-") and "-message" in model:
                parameters["intermediate_result_enabled"] = True
            if self.session.language != "auto":
                parameters["language_hints"] = [self.session.language]
        else:
            parameters.update(text_type="PlainText", voice=self.session.voice)
            if self.session.language != "auto":
                parameters["language_hints"] = [self.session.language]
        await self.send(
            "run-task",
            {
                "task_group": "audio",
                "task": "asr" if self.role == "stt" else "tts",
                "function": "recognition" if self.role == "stt" else "SpeechSynthesizer",
                "model": model,
                "parameters": parameters,
                "input": {},
            },
        )
        async with asyncio.timeout(self.config.VOICE_UPSTREAM_TIMEOUT):
            event = await self.read()
            if isinstance(event, bytes) or event["header"].get("event") != "task-started":
                raise SpeechProviderError("Expected inference task-started acknowledgement")
        self.ready.set()

    async def append_audio(self, pcm: bytes) -> None:
        async with asyncio.timeout(self.config.VOICE_UPSTREAM_TIMEOUT):
            await self.ready.wait()
            self.audio_samples += len(pcm) // 2
            await self.ws.send(pcm)

    async def commit_audio(self) -> None:
        async with asyncio.timeout(self.config.VOICE_UPSTREAM_TIMEOUT):
            await self.ready.wait()
            self.ready.clear()
            await self.send("finish-task", {"input": {}})

    def transcript_events(self, event: dict[str, Any]) -> None:
        payload = event.get("payload", {})
        if not isinstance(payload, dict) or not isinstance(payload.get("output", {}), dict):
            raise SpeechProviderError("Invalid inference transcript payload")
        sentence = payload.get("output", {}).get("sentence", {})
        if not isinstance(sentence, dict):
            raise SpeechProviderError("Invalid inference transcript")
        if not sentence or sentence.get("heartbeat"):
            return
        sentence_id = sentence.get("sentence_id", sentence.get("begin_time"))
        text = sentence.get("text", "")
        if sentence_id is None or not isinstance(text, str):
            raise SpeechProviderError("Inference transcript needs a stable sentence ID and text")
        item_id = f"{self.task_id}:{sentence_id}"
        if item_id in self.finished:
            return
        start_id = self.task_id if self.session.turn_detection == "manual" else item_id
        if start_id not in self.started:
            self.started.add(start_id)
            self.pending.append({"type": "input_audio_buffer.speech_started", "item_id": start_id})
        self.sentences[item_id] = text
        if sum(len(value) for value in self.sentences.values()) > 6000:
            raise SpeechProviderError("Inference transcript exceeds the input limit")
        final = sentence.get("sentence_end") is True
        if final:
            self.finished.add(item_id)
        if self.session.turn_detection == "manual":
            self.pending.append(
                {
                    "type": "conversation.item.input_audio_transcription.text",
                    "item_id": self.task_id,
                    "text": "".join(self.sentences.values()),
                }
            )
        elif final:
            timing: dict[str, int] = {}
            begin, end = sentence.get("begin_time"), sentence.get("end_time")
            if begin is not None and end is not None:
                if type(begin) is not int or type(end) is not int or begin < 0 or end < begin:
                    raise SpeechProviderError("Invalid inference sentence timing")
                timing = {
                    "audio_start_ms": self.task_audio_offset_ms + begin,
                    "audio_end_ms": self.task_audio_offset_ms + end,
                }
            self.pending.extend(
                [
                    {"type": "input_audio_buffer.speech_stopped", "item_id": item_id},
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": item_id,
                        "transcript": text,
                        **timing,
                    },
                ]
            )
            del self.sentences[item_id]
        else:
            self.pending.append(
                {
                    "type": "conversation.item.input_audio_transcription.text",
                    "item_id": item_id,
                    "text": text,
                }
            )

    async def receive(self, *, timeout: bool = True) -> dict[str, Any]:
        while not self.pending:
            event = await self.read(timeout=timeout)
            if isinstance(event, bytes):
                raise SpeechProviderError("Unexpected audio in recognition stream")
            kind = event["header"].get("event")
            if kind == "result-generated":
                self.transcript_events(event)
            elif kind == "task-finished":
                if self.ready.is_set():
                    raise SpeechProviderError("Recognition task ended without a commit")
                if self.session.turn_detection == "manual":
                    self.pending.extend(
                        [
                            {"type": "input_audio_buffer.speech_stopped", "item_id": self.task_id},
                            {
                                "type": "conversation.item.input_audio_transcription.completed",
                                "item_id": self.task_id,
                                "transcript": "".join(self.sentences.values()),
                                "audio_start_ms": self.task_audio_offset_ms,
                                "audio_end_ms": self.audio_samples // 16,
                            },
                        ]
                    )
                await self.start()
        return self.pending.popleft()

    async def synthesize(self, text: str, first_audio_timeout: float) -> AsyncIterator[bytes]:
        await self.start()
        await self.send("continue-task", {"input": {"text": text}})
        await self.send("finish-task", {"input": {}})
        deadline = asyncio.get_running_loop().time() + first_audio_timeout
        received_audio = False
        async with asyncio.timeout(120):
            while True:
                async with asyncio.timeout_at(None if received_audio else deadline):
                    event = await self.read()
                if isinstance(event, bytes):
                    if event:
                        received_audio = True
                        yield event
                elif event["header"].get("event") == "task-finished":
                    if not received_audio:
                        raise SpeechProviderError("Speech inference returned no audio")
                    return
