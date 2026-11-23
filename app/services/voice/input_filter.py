"""Assess voice input before it can create a chat turn or interrupt playback."""

import math
import re
import struct
import unicodedata
from collections import deque

from app.core.settings import Settings
from app.schemas.voice import SpeechEvidence, VoiceInputDecision

NON_ACTIONABLE_VOICE_TEXT = frozenset({"嗯", "嗯嗯", "呃", "额", "啊", "呢", "那"})
SHORT_VOICE_COMMANDS = frozenset({"ok", "okay", "yes", "no", "hi", "hey", "bye", "stop", "go"})


def normalize_voice_text(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", text)
        if not char.isspace() and unicodedata.category(char)[0] not in {"P", "C"}
    ).casefold()


def is_non_actionable_voice_text(text: str) -> bool:
    normalized = normalize_voice_text(text)
    return not any(char.isalnum() for char in normalized) or normalized in NON_ACTIONABLE_VOICE_TEXT


def assess_voice_input(
    text: str,
    language: str,
    config: Settings,
    *,
    evidence: SpeechEvidence | None = None,
    playback_text: str = "",
) -> VoiceInputDecision:
    if is_non_actionable_voice_text(text):
        return VoiceInputDecision(action="ignore", reason="non_speech")
    if evidence is not None and (
        evidence.duration_ms < config.VOICE_INPUT_MIN_SPEECH_MS
        or (
            evidence.active_ms is not None and evidence.active_ms < config.VOICE_INPUT_MIN_ACTIVE_MS
        )
    ):
        return VoiceInputDecision(action="review", reason="insufficient_audio")
    normalized = normalize_voice_text(text)
    words = re.findall(r"[A-Za-z]+", unicodedata.normalize("NFKC", text))
    if (
        language != "en"
        and re.fullmatch(r"[a-z]{1,4}", normalized)
        and (len(words) == 1 or all(len(word) == 1 for word in words))
        and normalized not in SHORT_VOICE_COMMANDS
    ) or (len(normalized) >= 4 and re.fullmatch(r"([a-z])\1+", normalized)):
        return VoiceInputDecision(action="review", reason="uncertain_text")
    if len(normalized) >= 6 and normalized in normalize_voice_text(playback_text):
        return VoiceInputDecision(action="review", reason="possible_echo")
    return VoiceInputDecision(action="accept")


class AudioActivity:
    """Keep bounded energy measurements, without retaining microphone audio."""

    def __init__(self, rms_dbfs: float) -> None:
        self.rms_dbfs = rms_dbfs
        self.samples = 0
        self.windows: deque[tuple[int, int, float]] = deque(maxlen=6000)

    def append(self, pcm: bytes) -> None:
        for offset in range(0, len(pcm), 640):
            chunk = pcm[offset : offset + 640]
            count = len(chunk) // 2
            power = sum(value * value for (value,) in struct.iter_unpack("<h", chunk))
            rms = 10 * math.log10(power / count / 32768**2) if power else -120.0
            end = self.samples + count
            self.windows.append((self.samples, end, rms))
            self.samples = end

    def evidence(
        self, start_ms: int, end_ms: int, *, rms_dbfs: float | None = None
    ) -> SpeechEvidence:
        evidence = SpeechEvidence(duration_ms=end_ms - start_ms)
        start = start_ms * 16
        end = end_ms * 16
        if not self.windows or start < self.windows[0][0] or end > self.samples:
            return evidence
        threshold = self.rms_dbfs if rms_dbfs is None else rms_dbfs
        active_samples = sum(
            max(0, min(end, window_end) - max(start, window_start))
            for window_start, window_end, rms in self.windows
            if rms >= threshold and window_end > start and window_start < end
        )
        return evidence.model_copy(update={"active_ms": active_samples / 16})
