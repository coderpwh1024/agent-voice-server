import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from app.core.settings import Settings
from app.schemas.voice import VoiceSession
from app.services.voice.providers.alibaba_inference import InferenceStream
from app.services.voice.providers.alibaba_realtime import AlibabaRealtime
from app.services.voice.providers.errors import SpeechProviderError


class Socket:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.task_id = ""
        self.closed = False

    def event(self, kind, **payload):
        self.events.put_nowait(
            json.dumps({"header": {"event": kind, "task_id": self.task_id}, "payload": payload})
        )

    async def send(self, raw):
        self.sent.append(raw)
        if isinstance(raw, str):
            data = json.loads(raw)
            if data["header"]["action"] == "run-task":
                self.task_id = data["header"]["task_id"]
                self.event("task-started")

    async def recv(self):
        return await self.events.get()


def session(**overrides):
    return VoiceSession(
        user_id="test-user",
        voice="longanhuan_v3.1",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        **overrides,
    )


def settings(**overrides):
    return Settings(
        _env_file=None,
        USE_FAKE_MODEL=True,
        DASHSCOPE_API_KEY=SecretStr("test-key"),
        VOICE_REALTIME_STT_MODEL="qwen-audio-3.1-asr-flash-message",
        VOICE_REALTIME_TTS_MODEL="qwen-audio-3.1-tts-flash",
        **overrides,
    )


@pytest.mark.asyncio
async def test_inference_connection_routes_models_without_query_and_closes_on_exit(monkeypatch):
    socket = Socket()
    endpoints = []

    @asynccontextmanager
    async def connect(endpoint, **kwargs):
        endpoints.append(endpoint)
        try:
            yield socket
        finally:
            socket.closed = True

    monkeypatch.setattr("app.services.voice.providers.alibaba_realtime.connect", connect)
    provider = AlibabaRealtime(settings())
    async with provider.recognize(session()) as stream:
        await provider.append_audio(stream, b"\0\0" * 320)
        assert isinstance(socket.sent[-1], bytes)
        upload = json.loads(socket.sent[0])
        assert upload["payload"]["model"] == "qwen-audio-3.1-asr-flash-message"
        assert upload["payload"]["parameters"]["sample_rate"] == 16000
        assert upload["payload"]["parameters"]["intermediate_result_enabled"] is True
        assert upload["payload"]["parameters"]["language_hints"] == ["zh"]
    assert endpoints == ["wss://dashscope.aliyuncs.com/api-ws/v1/inference"]
    assert socket.closed


@pytest.mark.asyncio
async def test_streaming_asr_receives_noise_threshold_and_near_field_vad():
    socket = Socket()
    config = settings(VOICE_VAD_THRESHOLD=0.45)
    config.VOICE_REALTIME_STT_MODEL = "qwen-audio-3.1-asr-flash-streaming"
    await InferenceStream(socket, session(), config, "stt").start()
    parameters = json.loads(socket.sent[0])["payload"]["parameters"]
    assert parameters["speech_noise_threshold"] == 0.45
    assert parameters["vad_model"] == "near_meeting_16k"


@pytest.mark.asyncio
async def test_inference_preserves_audio_timing_across_task_restarts():
    socket = Socket()
    stream = InferenceStream(socket, session(), settings(), "stt")
    await stream.start()
    await stream.append_audio(b"\0\0" * 16000)
    await stream.start()
    stream.transcript_events(
        {
            "payload": {
                "output": {
                    "sentence": {
                        "sentence_id": 1,
                        "text": "你好",
                        "sentence_end": True,
                        "begin_time": 100,
                        "end_time": 500,
                    }
                }
            }
        }
    )
    final = list(stream.pending)[-1]
    assert final["audio_start_ms"] == 1100
    assert final["audio_end_ms"] == 1500


@pytest.mark.asyncio
@pytest.mark.parametrize("begin,end", [(-1, 5), (10, 5), (True, 10), ("0", 10)])
async def test_inference_rejects_invalid_sentence_timing(begin, end):
    socket = Socket()
    stream = InferenceStream(socket, session(), settings(), "stt")
    await stream.start()
    with pytest.raises(SpeechProviderError, match="timing"):
        stream.transcript_events(
            {
                "payload": {
                    "output": {
                        "sentence": {
                            "sentence_id": 1,
                            "text": "你好",
                            "sentence_end": True,
                            "begin_time": begin,
                            "end_time": end,
                        }
                    }
                }
            }
        )


@pytest.mark.asyncio
async def test_transcripts_are_replaced_deduplicated_and_keep_unique_task_ids():
    socket = Socket()
    stream = InferenceStream(socket, session(), settings(), "stt")
    await stream.start()
    old_task = stream.task_id
    for text, final in [("你", False), ("你好", False), ("你好。", True), ("你好。", True)]:
        stream.transcript_events(
            {
                "payload": {
                    "output": {"sentence": {"sentence_id": 1, "text": text, "sentence_end": final}}
                }
            }
        )
    events = list(stream.pending)
    assert [e["text"] for e in events if e["type"].endswith(".text")] == ["你", "你好"]
    finals = [e for e in events if e["type"].endswith(".completed")]
    assert [e["transcript"] for e in finals] == ["你好。"]
    assert finals[0]["item_id"] == old_task + ":1"
    assert events[0]["type"] == "input_audio_buffer.speech_started"
    assert events[-2]["type"] == "input_audio_buffer.speech_stopped"


@pytest.mark.asyncio
async def test_manual_commit_aggregates_sentences_then_restarts_without_losing_audio():
    socket = Socket()
    stream = InferenceStream(socket, session(turn_detection="manual"), settings(), "stt")
    await stream.start()
    old_task = stream.task_id
    for number, text in [(1, "你好。"), (2, "请帮我查天气。")]:
        stream.transcript_events(
            {
                "payload": {
                    "output": {
                        "sentence": {"sentence_id": number, "text": text, "sentence_end": True}
                    }
                }
            }
        )
    assert all(not e["type"].endswith(".completed") for e in stream.pending)
    stream.pending.clear()
    await stream.commit_audio()
    queued_audio = asyncio.create_task(stream.append_audio(b"\0\0"))
    await asyncio.sleep(0)
    assert not queued_audio.done()
    socket.event("task-finished")
    assert (await stream.receive())["type"] == "input_audio_buffer.speech_stopped"
    final = await stream.receive()
    assert final["transcript"] == "你好。请帮我查天气。"
    assert final["item_id"] == old_task
    assert stream.task_id != old_task
    await asyncio.wait_for(queued_audio, 1)
    assert socket.sent[-1] == b"\0\0"


@pytest.mark.asyncio
async def test_tts_streams_pcm_and_reuses_connection_for_distinct_segment_tasks():
    socket = Socket()
    stream = InferenceStream(socket, session(), settings(), "tts")
    ids = []
    for text in ["第一句话。", "第二句话。"]:
        output = stream.synthesize(text, 1)
        first = asyncio.create_task(anext(output))
        await asyncio.sleep(0)
        ids.append(stream.task_id)
        socket.events.put_nowait(b"\0\0" * 480)
        socket.event("task-finished")
        assert await first == b"\0\0" * 480
        assert [part async for part in output] == []
    assert len(set(ids)) == 2
    messages = [json.loads(value) for value in socket.sent]
    assert [m["header"]["action"] for m in messages] == [
        "run-task",
        "continue-task",
        "finish-task",
    ] * 2
    assert messages[0]["payload"]["parameters"]["voice"] == "longanhuan_v3.1"
    assert messages[0]["payload"]["parameters"]["sample_rate"] == 24000
    assert messages[1]["payload"]["input"]["text"] == "第一句话。"
    assert len({m["header"]["task_id"] for m in messages[:3]}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["task-failed", "wrong-task", "malformed", "odd-pcm", "empty-audio"]
)
async def test_tts_rejects_provider_failures_and_malformed_audio(failure):
    socket = Socket()
    stream = InferenceStream(socket, session(), settings(), "tts")
    output = stream.synthesize("测试。", 1)
    first = asyncio.create_task(anext(output))
    await asyncio.sleep(0)
    if failure == "task-failed":
        socket.event("task-failed")
    elif failure == "wrong-task":
        socket.events.put_nowait(
            json.dumps({"header": {"event": "task-finished", "task_id": "wrong"}})
        )
    elif failure == "malformed":
        socket.events.put_nowait("not-json")
    elif failure == "odd-pcm":
        socket.events.put_nowait(b"x")
    else:
        socket.event("task-finished")
    with pytest.raises(SpeechProviderError):
        await first


@pytest.mark.asyncio
async def test_tts_first_audio_deadline_is_not_reset_by_metadata():
    socket = Socket()
    stream = InferenceStream(socket, session(), settings(), "tts")
    first = asyncio.create_task(anext(stream.synthesize("测试。", 0.02)))
    await asyncio.sleep(0)
    socket.event("result-generated", output={})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(first, 0.2)


@pytest.mark.asyncio
async def test_tts_provider_error_keeps_safe_code_without_echoing_input():
    socket = Socket()
    stream = InferenceStream(socket, session(), settings(), "tts")
    first = asyncio.create_task(anext(stream.synthesize("合成测试。", 1)))
    await asyncio.sleep(0)
    socket.events.put_nowait(
        json.dumps(
            {
                "header": {
                    "event": "task-failed",
                    "task_id": socket.task_id,
                    "error_code": "InvalidParameter",
                    "error_message": "private provider details",
                },
                "payload": {},
            }
        )
    )
    with pytest.raises(SpeechProviderError) as caught:
        await first
    assert caught.value.code == "InvalidParameter"
    assert caught.value.retryable is False
    assert "private provider details" not in str(caught.value)


@pytest.mark.asyncio
async def test_cancelled_recognition_closes_the_upstream_connection(monkeypatch):
    socket = Socket()
    entered = asyncio.Event()

    @asynccontextmanager
    async def connect(endpoint, **kwargs):
        try:
            yield socket
        finally:
            socket.closed = True

    monkeypatch.setattr("app.services.voice.providers.alibaba_realtime.connect", connect)
    provider = AlibabaRealtime(settings())

    async def listen():
        async with provider.recognize(session()) as stream:
            entered.set()
            await provider.receive(stream, timeout=False)

    task = asyncio.create_task(listen())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed
