import struct

import pytest

from app.core.settings import Settings
from app.schemas.voice import SpeechEvidence
from app.services.voice.input_filter import AudioActivity, assess_voice_input


@pytest.mark.parametrize("text", ["KK.", "Q", "C.", "XY", "ＫＫ。", "K K", "aaaaaa"])
def test_ambiguous_fragments_require_review_without_a_word_blacklist(text):
    decision = assess_voice_input(text, "zh", Settings(_env_file=None))
    assert decision.action == "review"
    assert decision.reason == "uncertain_text"


@pytest.mark.parametrize("text", ["。", "…", "😶", "\u200b\u2060", "嗯。", "——"])
def test_non_speech_does_not_create_a_review_or_chat_turn(text):
    assert assess_voice_input(text, "zh", Settings(_env_file=None)).action == "ignore"


@pytest.mark.parametrize(
    "text,language",
    [
        ("停", "zh"),
        ("好的", "zh"),
        ("OK!", "zh"),
        ("Hi", "zh"),
        ("yes", "zh"),
        ("no", "zh"),
        ("Q 是什么？", "zh"),
        ("C++", "zh"),
        ("I am", "zh"),
        ("OpenAI", "zh"),
        ("chat", "en"),
        ("Q", "en"),
    ],
)
def test_short_commands_and_meaningful_english_are_preserved(text, language):
    assert assess_voice_input(text, language, Settings(_env_file=None)).action == "accept"


@pytest.mark.parametrize(
    "evidence",
    [SpeechEvidence(duration_ms=60), SpeechEvidence(duration_ms=500, active_ms=20)],
)
def test_impulse_or_silent_audio_requires_review_in_every_language(evidence):
    for language in ["zh", "en", "auto"]:
        decision = assess_voice_input(
            "请查询天气", language, Settings(_env_file=None), evidence=evidence
        )
        assert decision.action == "review"
        assert decision.reason == "insufficient_audio"


def test_echo_review_only_applies_to_text_still_being_played():
    config = Settings(_env_file=None)
    decision = assess_voice_input(
        "今天可以去公园走走。", "zh", config, playback_text="好的，今天可以去公园走走。"
    )
    assert decision.action == "review" and decision.reason == "possible_echo"
    assert assess_voice_input("今天可以去公园走走。", "zh", config).action == "accept"
    assert (
        assess_voice_input("好的", "zh", config, playback_text="好的。今天去公园").action
        == "accept"
    )


def test_audio_evidence_tracks_energy_and_sentence_boundaries_without_retaining_pcm():
    activity = AudioActivity(-55)
    activity.append(b"\0\0" * 1600)
    activity.append(struct.pack("<h", 1000) * 1600)
    activity.append(struct.pack("<h", 25) * 1600)
    assert activity.evidence(0, 300) == SpeechEvidence(duration_ms=300, active_ms=100)
    assert activity.evidence(90, 150) == SpeechEvidence(duration_ms=60, active_ms=50)
    assert activity.evidence(200, 300) == SpeechEvidence(duration_ms=100, active_ms=0)
    assert activity.evidence(200, 300, rms_dbfs=-67).active_ms == 100
    assert activity.evidence(0, 100, rms_dbfs=-67).active_ms == 0
    assert activity.evidence(0, 400).active_ms is None
    assert all(isinstance(window[2], float) for window in activity.windows)


def test_long_audio_keeps_bounded_measurements_and_unknown_old_evidence():
    activity = AudioActivity(-55)
    for _ in range(6100):
        activity.append(b"\0\0" * 320)
    assert len(activity.windows) == 6000
    assert activity.evidence(0, 100).active_ms is None
    assert activity.evidence(121_000, 121_500).active_ms == 0
