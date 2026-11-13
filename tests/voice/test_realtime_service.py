import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import MemorySaver
from langgraph.func import entrypoint
from langgraph.graph import END, MessagesState, StateGraph
from langgraph.types import interrupt
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from app.core import settings
from app.main import app
from app.schemas.voice import ZERO_UUID, VoiceOption, VoiceTurn, decode_audio, encode_audio
from app.services.location import device_location
from app.services.voice.providers.errors import SpeechProviderError


class MemoryRepository:
    def __init__(self):
        self.sessions = {}
        self.records = {}
        self.owners = {}
        self.runs = {}
        self.locks = set()

    @asynccontextmanager
    async def guard(self, key):
        if key in self.locks:
            raise HTTPException(409, "busy")
        self.locks.add(key)
        try:
            yield
        finally:
            self.locks.remove(key)

    async def owner(self, thread_id):
        return self.owners.get(thread_id)

    async def claim(self, thread_id, user_id, agent_id):
        self.owners.setdefault(thread_id, (user_id, agent_id))
        if self.owners[thread_id] != (user_id, agent_id):
            raise HTTPException(403, "wrong owner")

    async def register_run(self, run_id, user_id, thread_id):
        self.runs.setdefault(run_id, (user_id, thread_id))

    async def owns_run(self, run_id, user_id):
        return self.runs.get(run_id, (None, None))[0] == user_id

    async def save_session(self, session):
        self.sessions[session.session_id] = session.model_copy(deep=True)

    async def get_session(self, session_id):
        session = self.sessions.get(session_id)
        return session.model_copy(deep=True) if session else None

    async def active_sessions(self, user_id):
        return sum(
            s.user_id == user_id and s.status in {"created", "connected"}
            for s in self.sessions.values()
        )

    async def save_turn(self, turn):
        self.records[turn.turn_id] = turn.model_copy(deep=True)

    async def has_input(self, session_id, input_id):
        return any(
            t.session_id == session_id and t.input_id == input_id for t in self.records.values()
        )

    async def turns(self, thread_id, limit=50):
        return [t.model_copy(deep=True) for t in self.records.values() if t.thread_id == thread_id][
            -limit:
        ]


class FakeSpeech:
    def __init__(self, config):
        self.events = asyncio.Queue()

    @asynccontextmanager
    async def recognize(self, session):
        yield self

    @asynccontextmanager
    async def synthesizer(self, session):
        yield self

    async def append_audio(self, ws, pcm):
        await self.events.put({"type": "input_audio_buffer.speech_started", "item_id": "speech-1"})
        await self.events.put(
            {
                "type": "conversation.item.input_audio_transcription.text",
                "item_id": "speech-1",
                "text": "你",
                "stash": "好",
            }
        )
        for _ in range(2):
            await self.events.put(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "speech-1",
                    "transcript": "你好",
                }
            )

    async def commit_audio(self, ws):
        await self.append_audio(ws, b"\0\0")

    async def receive(self, ws, timeout=False):
        return await self.events.get()

    async def synthesize(self, ws, text):
        yield b"\0\0" * 480
        await asyncio.sleep(0.002)
        yield b"\0\0" * 480


class SlowFirstSpeech(FakeSpeech):
    def __init__(self, config):
        super().__init__(config)
        self.synthesis_calls = 0

    async def synthesize(self, ws, text):
        self.synthesis_calls += 1
        if self.synthesis_calls == 1:
            await asyncio.sleep(2.1)
        yield b"\0\0" * 480


class FailingSpeech(FakeSpeech):
    async def synthesize(self, ws, text):
        await asyncio.sleep(0.05)
        if False:
            yield b""
        raise RuntimeError("synthetic TTS failure")


class WakeSpeech(FakeSpeech):
    transcript = "小美，小美，介绍一下你自己"

    async def append_audio(self, ws, pcm):
        await self.events.put(
            {"type": "input_audio_buffer.speech_started", "item_id": "wake-speech"}
        )
        await self.events.put(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "wake-speech",
                "transcript": self.transcript,
            }
        )


class RejectedWakeSpeech(FakeSpeech):
    async def append_audio(self, ws, pcm):
        await self.events.put(
            {"type": "input_audio_buffer.speech_started", "item_id": "false-wake"}
        )
        await self.events.put(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "false-wake",
                "transcript": "今天天气不错",
            }
        )


class SegmentedWakeSpeech(FakeSpeech):
    transcripts = ("小美", "小美", "小美小美小美", "小美，小美，介绍一下你自己")

    def __init__(self, config):
        super().__init__(config)
        self.index = 0

    async def append_audio(self, ws, pcm):
        transcript = self.transcripts[self.index]
        self.index += 1
        await self.events.put(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": f"wake-{self.index}",
                "transcript": transcript,
            }
        )


class EchoFillerSpeech(FakeSpeech):
    second_transcript = "嗯"

    def __init__(self, config):
        super().__init__(config)
        self.utterances = 0

    async def append_audio(self, ws, pcm):
        self.utterances += 1
        item_id = f"speech-{self.utterances}"
        transcript = "你好" if self.utterances == 1 else self.second_transcript
        await self.events.put({"type": "input_audio_buffer.speech_started", "item_id": item_id})
        await self.events.put(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": item_id,
                "transcript": transcript,
            }
        )


def build_chatbot(responses=None, sleep=0):
    model = FakeListChatModel(responses=responses or ["你好。这是一段语音回答。"], sleep=sleep)

    @entrypoint(checkpointer=MemorySaver())
    async def chatbot(inputs, *, previous, config):
        messages = (previous or {}).get("messages", []) + inputs["messages"]
        response = await model.ainvoke(messages, config)
        return entrypoint.final(
            value={"messages": [response]}, save={"messages": messages + [response]}
        )

    return chatbot


def location_chatbot(captured):
    @entrypoint(checkpointer=MemorySaver())
    async def chatbot(inputs, *, previous, config):
        captured.append(device_location())
        response = AIMessage(content="收到位置")
        messages = (previous or {}).get("messages", []) + inputs["messages"]
        return entrypoint.final(
            value={"messages": [response]}, save={"messages": messages + [response]}
        )

    return chatbot


def test_text_device_location_is_one_turn_only(voice_env):
    client, repo, agents, headers = voice_env
    captured = []
    agents["chatbot"] = location_chatbot(captured)
    auth = headers()
    session = create(client, auth, input_mode="text")
    fix = {
        "latitude": 39.9,
        "longitude": 116.4,
        "accuracy_meters": 20,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="我现在在哪？", device_location=fix)
        read_until(ws, "response.done")
        event(ws, "input.text", text="你好")
        read_until(ws, "response.done")
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert captured[0].longitude == 116.4
    assert captured[1] is None
    assert all("116.4" not in str(turn.model_dump()) for turn in repo.records.values())


@pytest.mark.parametrize("status", ["ok", "denied", "stale"])
def test_foreground_location_updates_are_scoped_to_connection(voice_env, status):
    client, repo, agents, headers = voice_env
    captured = []
    agents["chatbot"] = location_chatbot(captured)
    auth = headers()
    session = create(client, auth, input_mode="text")
    fix = {
        "latitude": 39.9,
        "longitude": 116.4,
        "accuracy_meters": 20,
        "captured_at": (
            datetime.now(UTC) - timedelta(minutes=6 if status == "stale" else 0)
        ).isoformat(),
    }
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        assert ready["location_updates"] is True
        event(ws, "location.update", status="ok", device_location=fix)
        if status == "denied":
            event(ws, "location.update", status="denied")
        event(ws, "input.text", text="附近有哪些商场？")
        read_until(ws, "response.done")
        event(ws, "input.text", text="你好")
        read_until(ws, "response.done")
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert (captured[0] is not None) == (status == "ok")
    assert captured[1] is None
    assert all("116.4" not in str(turn.model_dump()) for turn in repo.records.values())


@pytest.mark.parametrize("status", ["ok", "denied", "timeout"])
def test_asr_requests_location_and_accepts_result(voice_env, monkeypatch, status):
    import app.api.voice as voice_routes

    class LocationSpeech(FakeSpeech):
        async def append_audio(self, ws, pcm):
            await self.events.put(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "location-speech",
                    "transcript": "我现在在哪？",
                }
            )

    monkeypatch.setattr(voice_routes, "AlibabaRealtime", LocationSpeech)
    client, _repo, agents, headers = voice_env
    captured = []
    agents["chatbot"] = location_chatbot(captured)
    if status == "timeout":
        monkeypatch.setattr(settings, "VOICE_LOCATION_TIMEOUT_SECONDS", 0.05)
    auth = headers()
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        ws.send_bytes(
            encode_audio(b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, 0, kind=1)
        )
        request = read_until(ws, "location.request")[-1]
        payload = (
            {
                "device_location": {
                    "latitude": 39.9,
                    "longitude": 116.4,
                    "accuracy_meters": 20,
                    "captured_at": datetime.now(UTC).isoformat(),
                }
            }
            if status == "ok"
            else {}
        )
        if status != "timeout":
            event(ws, "location.result", request_id=request["request_id"], status=status, **payload)
        assert read_until(ws, "response.done")[-1]["execution_status"] == "completed"
        event(ws, "session.close")
        read_until(ws, "session.closed")
    if status == "ok":
        assert captured[0].longitude == 116.4
    else:
        assert captured == [None]


@pytest.mark.parametrize("status", ["ok", "denied"])
def test_asr_reuses_foreground_location_without_requesting_another_fix(
    voice_env, monkeypatch, status
):
    import app.api.voice as voice_routes

    class LocationSpeech(FakeSpeech):
        async def append_audio(self, ws, pcm):
            await self.events.put(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "nearby-speech",
                    "transcript": "附近有哪些商场？",
                }
            )

    monkeypatch.setattr(voice_routes, "AlibabaRealtime", LocationSpeech)
    monkeypatch.setattr(settings, "VOICE_LOCATION_TIMEOUT_SECONDS", 0.05)
    client, _repo, agents, headers = voice_env
    captured = []
    agents["chatbot"] = location_chatbot(captured)
    auth = headers()
    session = create(client, auth)
    payload = (
        {
            "device_location": {
                "latitude": 39.9,
                "longitude": 116.4,
                "accuracy_meters": 20,
                "captured_at": datetime.now(UTC).isoformat(),
            }
        }
        if status == "ok"
        else {}
    )
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        event(ws, "location.update", status=status, **payload)
        ws.send_bytes(
            encode_audio(b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, 0, kind=1)
        )
        events = read_until(ws, "response.done")
        assert not any(item["type"] == "location.request" for item in events)
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert (captured[0] is not None) == (status == "ok")


@pytest.fixture
def voice_env(monkeypatch):
    import app.api.agui as agui
    import app.api.chats as service
    import app.api.voice as voice_routes

    repo = MemoryRepository()
    agents = {"chatbot": build_chatbot()}
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("test-admin-secret"))
    monkeypatch.setattr(settings, "APP_TOKEN_SECRET", SecretStr("a" * 48))
    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "VOICE_REALTIME_VOICES",
        [
            VoiceOption(id="Cherry", name="芊悦", description="阳光积极、亲切自然"),
            VoiceOption(id="Serena", name="苏瑶", description="温柔自然"),
        ],
    )
    monkeypatch.setattr(settings, "VOICE_SESSION_SECONDS", 60)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", FakeSpeech)
    for module in (service, voice_routes, agui):
        monkeypatch.setattr(module, "get_agent", lambda name: agents[name])
    monkeypatch.setattr(app.state, "voice_repository", repo, raising=False)
    monkeypatch.setattr(app.state, "voice_connections", {}, raising=False)
    client = TestClient(app)

    def headers(user="alice"):
        response = client.post(
            "/auth/token",
            json={"user_id": user},
            headers={"Authorization": "Bearer test-admin-secret"},
        )
        assert response.status_code == 200
        return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}

    yield client, repo, agents, headers
    client.close()


def create(client, headers, **body):
    response = client.post("/voice/sessions", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_unknown_voice_agent_preserves_error_response(voice_env, monkeypatch):
    import app.api.voice as voice_routes
    from app.api.dependencies import get_agent

    client, repo, agents, headers = voice_env
    monkeypatch.setattr(voice_routes, "get_agent", get_agent)
    response = client.post("/voice/sessions", headers=headers(), json={"agent_id": "unknown-agent"})
    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "Agent not found", "data": None}
    assert not repo.sessions


def test_model_upgrade_advertises_bound_voices_and_rejects_stale_selection(voice_env, monkeypatch):
    client, repo, agents, headers = voice_env
    auth = headers()
    previous = create(client, auth, voice="Cherry")
    monkeypatch.setattr(settings, "VOICE_REALTIME_TTS_MODEL", "qwen-audio-3.1-tts-flash")
    data = client.get("/voice/capabilities", headers=auth).json()["data"]
    assert data["voice_catalog_model"] == "qwen-audio-3.1-tts-flash"
    assert data["default_voice"] == "longanlingxin_v3.1"
    assert "Cherry" not in data["voices"]
    assert data["input_format"] == "pcm16_16000_mono"
    assert data["output_format"] == "pcm16_24000_mono"
    assert client.post("/voice/sessions", headers=auth, json={"voice": "Cherry"}).status_code == 422
    assert create(client, auth)["voice"] == data["default_voice"]
    with pytest.raises(WebSocketDisconnect) as caught:
        with connect(client, previous, auth):
            pytest.fail("A session with an obsolete voice must not connect")
    assert caught.value.code == 4409


def test_voice_language_is_checked_against_the_model_catalog(voice_env, monkeypatch):
    client, repo, agents, headers = voice_env
    monkeypatch.setattr(settings, "VOICE_REALTIME_TTS_MODEL", "qwen-audio-3.1-tts-flash")
    auth = headers()
    response = client.post(
        "/voice/sessions", headers=auth, json={"voice": "yuxiaoyun_v3.1", "language": "en"}
    )
    assert response.status_code == 422
    assert create(client, auth, voice="yuxiaoyun_v3.1", language="zh")["voice"] == "yuxiaoyun_v3.1"


def event(ws, kind, **data):
    ws.send_json({"type": kind, "event_id": str(uuid4()), **data})


def read_until(ws, kind):
    seen = []
    for _ in range(2000):
        raw = ws.receive()
        if raw.get("bytes") is not None:
            seen.append({"type": "audio", "frame": decode_audio(raw["bytes"])})
        elif raw.get("text") is not None:
            item = json.loads(raw["text"])
            seen.append(item)
            if item["type"] == kind:
                return seen
            assert item["type"] != "session.closed", item
        else:
            pytest.fail(str(raw))
    pytest.fail(f"Missing event: {kind}")


def connect(client, session, headers):
    return client.websocket_connect(f"/voice/sessions/{session['session_id']}/ws", headers=headers)


def configure(ws):
    event(ws, "session.configure")
    return read_until(ws, "session.ready")[-1]


def test_device_memory_roundtrip_is_bound_to_response_and_not_saved(voice_env):
    from app.services.memory.local_context import device_memory

    client, repo, agents, headers = voice_env

    @entrypoint(checkpointer=MemorySaver())
    async def local_agent(inputs, *, previous, config):
        value = device_memory(
            config["configurable"]["user_id"], config["configurable"]["thread_id"]
        )
        answer = AIMessage(content=f"已收到 {len(value.context.memories)} 条参考")
        return entrypoint.final(
            value={"messages": [answer]}, save={"messages": inputs["messages"] + [answer]}
        )

    agents["chatbot"] = local_agent
    auth = headers()
    session = create(client, auth, input_mode="text", memory_mode="local")
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="按我的习惯回答")
        events = read_until(ws, "memory.context.request")
        request = events[-1]
        assert request["text"] == "按我的习惯回答"
        accepted = next(item for item in events if item["type"] == "input.accepted")
        assert accepted["memory_eligible"] is True
        assert accepted["turn_id"] == request["source_id"]
        event(
            ws,
            "memory.context",
            response_id=request["response_id"],
            context={
                "revision": 4,
                "memories": [{"id": str(uuid4()), "category": "profile", "text": "本机独有参考"}],
            },
        )
        done = read_until(ws, "response.done")
        assert done[-1]["execution_status"] == "completed"
        event(ws, "session.close")
        read_until(ws, "session.closed")
    history = client.post(
        "/chatbot/history", headers=auth, json={"thread_id": session["thread_id"]}
    ).json()
    assert "已收到 1 条参考" in str(history)
    assert "本机独有参考" not in str(history)


def test_device_memory_timeout_finishes_without_server_memory_fallback(voice_env, monkeypatch):
    client, repo, agents, headers = voice_env
    monkeypatch.setattr(settings, "LOCAL_MEMORY_CONTEXT_TIMEOUT_SECONDS", 0.1)
    auth = headers()
    session = create(client, auth, input_mode="text", memory_mode="local")
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="你好")
        events = read_until(ws, "response.done")
        assert any(item["type"] == "memory.context.unavailable" for item in events)
        assert events[-1]["execution_status"] == "completed"
        event(ws, "memory.context", response_id=events[-1]["response_id"], context={})
        assert read_until(ws, "error")[-1]["code"] == "stale_memory_context"
        event(ws, "session.close")
        read_until(ws, "session.closed")


def test_text_mode_skips_asr_and_keeps_agent_tts_and_history(voice_env, monkeypatch):
    import app.api.voice as voice_routes

    class NoRecognitionSpeech(FakeSpeech):
        @asynccontextmanager
        async def recognize(self, session):
            raise SpeechProviderError("ASR is unavailable")
            yield self

    monkeypatch.setattr(voice_routes, "AlibabaRealtime", NoRecognitionSpeech)
    client, repo, agents, headers = voice_env
    auth = headers()
    session = create(client, auth, input_mode="text")
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        assert ready["input_mode"] == "text"
        ws.send_bytes(b"unexpected microphone data")
        assert read_until(ws, "error")[-1]["code"] == "audio_input_disabled"
        event(ws, "input.commit")
        assert read_until(ws, "error")[-1]["code"] == "audio_input_disabled"
        event(ws, "input.text", text="键盘输入")
        seen = read_until(ws, "response.done")
        assert any(item["type"] == "audio" for item in seen)
        assert seen[-1]["execution_status"] == "completed"
        assert seen[-1]["audio_status"] == "completed"
        event(ws, "playback.finished", response_id=seen[-1]["response_id"])
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert [turn.input_text for turn in repo.records.values()] == ["键盘输入"]
    history = client.post(
        "/chatbot/history", json={"thread_id": session["thread_id"]}, headers=auth
    ).json()["data"]["messages"]
    assert [message["type"] for message in history] == ["human", "ai"]
    assert history[0]["content"] == "键盘输入"


@pytest.mark.parametrize("transcript", ["你好", "嗯", ""])
def test_dictation_only_runs_agent_after_explicit_text_send(voice_env, monkeypatch, transcript):
    import app.api.voice as voice_routes

    class DictationSpeech(FakeSpeech):
        async def commit_audio(self, ws):
            for _ in range(2):
                await self.events.put(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "dictation",
                        "transcript": transcript,
                    }
                )

        async def synthesize(self, ws, text):
            raise AssertionError("Dictation must not synthesize speech")
            yield b""

    monkeypatch.setattr(voice_routes, "AlibabaRealtime", DictationSpeech)
    client, repo, agents, headers = voice_env
    auth = headers()
    session = create(client, auth, input_mode="transcription", turn_detection="manual")
    with connect(client, session, auth) as ws:
        assert configure(ws)["input_mode"] == "transcription"
        event(ws, "input.text", text="不可自动执行")
        assert read_until(ws, "error")[-1]["code"] == "transcription_only"
        event(ws, "memory.confirm", confirmation_id="a" * 64, approved=True)
        assert read_until(ws, "error")[-1]["code"] == "transcription_only"
        event(ws, "input.commit")
        seen = read_until(ws, "transcript.final")
        assert seen[-1]["text"] == transcript
        event(ws, "ping")
        seen += read_until(ws, "pong")
        assert sum(item["type"] == "transcript.final" for item in seen) == 1
        assert not repo.records
        assert not any(item["type"] in {"input.accepted", "text.delta", "audio"} for item in seen)
        event(ws, "session.close")
        read_until(ws, "session.closed")
    history = client.post(
        "/chatbot/history", json={"thread_id": session["thread_id"]}, headers=auth
    ).json()["data"]["messages"]
    assert history == []
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", FakeSpeech)
    text_session = create(client, auth, input_mode="text", thread_id=session["thread_id"])
    with connect(client, text_session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text=transcript + "，已编辑")
        seen = read_until(ws, "response.done")
        assert any(item["type"] == "audio" for item in seen)
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert [turn.input_text for turn in repo.records.values()] == [transcript + "，已编辑"]


@pytest.mark.parametrize(
    "body",
    [
        {"input_mode": "unknown"},
        {"input_mode": "transcription"},
        {"input_mode": "text", "activation": "wake_word", "wake_word": "小美小美"},
        {
            "input_mode": "transcription",
            "turn_detection": "manual",
            "activation": "wake_word",
            "wake_word": "小美小美",
        },
    ],
)
def test_input_modes_reject_incompatible_configuration(voice_env, body):
    client, repo, agents, headers = voice_env
    assert client.post("/voice/sessions", headers=headers(), json=body).status_code == 422
    assert not repo.sessions


def test_input_modes_preserve_default_and_session_ownership(voice_env):
    client, repo, agents, headers = voice_env
    auth = headers()
    modes = client.get("/voice/capabilities", headers=auth).json()["data"]["input_modes"]
    assert modes == ["conversation", "text", "transcription"]
    session = create(client, auth)
    assert session["input_mode"] == "conversation"
    for mode in ("text", "transcription"):
        body = {"input_mode": mode, "thread_id": session["thread_id"], "turn_detection": "manual"}
        assert client.post("/voice/sessions", headers=headers("bob"), json=body).status_code == 403


def test_voice_roundtrip_streams_and_persists(voice_env):
    client, repo, agents, headers = voice_env
    auth = headers()
    session = create(client, auth)
    assert session["voice"] == "Cherry"
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        frame = encode_audio(b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, 0, kind=1)
        ws.send_bytes(frame)
        events = read_until(ws, "response.done")
        assert [e["text"] for e in events if e["type"] == "transcript.partial"] == ["你好"]
        assert sum(e["type"] == "transcript.final" for e in events) == 1
        audio = [e for e in events if e["type"] == "audio"]
        assert audio and all(e["frame"][1] == ready["connection_id"] for e in audio)
        assert (
            "".join(e["text"] for e in events if e["type"] == "text.delta")
            == "你好。这是一段语音回答。"
        )
        response_id = events[-1]["response_id"]
        event(ws, "playback.finished", response_id=response_id)
        event(ws, "ping")
        read_until(ws, "pong")
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert len(repo.records) == 1
    turn = next(iter(repo.records.values()))
    assert turn.status == "completed"
    assert turn.audio_status == "completed"
    assert all(s.played_samples == s.samples > 0 for s in turn.segments)
    history = client.post(
        "/chatbot/history", json={"thread_id": session["thread_id"]}, headers=auth
    )
    assert history.status_code == 200
    assert [m["type"] for m in history.json()["data"]["messages"]] == ["human", "ai"]


@pytest.mark.parametrize(
    ("status", "interrupted", "error_code", "audio_status"),
    [
        ("completed", False, None, "completed"),
        ("failed", False, "agent_failed", "completed"),
        ("completed", False, "tts_backpressure", "failed"),
        ("cancelled", True, None, "cancelled"),
    ],
)
def test_legacy_voice_turn_infers_audio_status(status, interrupted, error_code, audio_status):
    turn = VoiceTurn.model_validate(
        {
            "session_id": str(uuid4()),
            "thread_id": str(uuid4()),
            "input_id": str(uuid4()),
            "input_text": "legacy",
            "status": status,
            "execution_status": status,
            "interrupted": interrupted,
            "error_code": error_code,
        }
    )

    assert turn.audio_status == audio_status


def test_slow_tts_applies_backpressure_without_truncating_audio(voice_env, monkeypatch):
    import app.api.voice as voice_routes

    client, repo, agents, headers = voice_env
    response = "".join(f"这是第{index}句。" for index in range(1, 21))
    agents["chatbot"] = build_chatbot([response])
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", SlowFirstSpeech)
    auth = headers()
    session = create(client, auth)

    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="请完整朗读")
        events = read_until(ws, "response.done")
        event(ws, "session.close")
        read_until(ws, "session.closed")

    done = events[-1]
    completed_segments = [item for item in events if item["type"] == "audio.segment.done"]
    assert "".join(item["text"] for item in events if item["type"] == "text.delta") == response
    assert 1 < len(completed_segments) <= 20
    assert done["status"] == "completed"
    assert done["execution_status"] == "completed"
    assert done["audio_status"] == "completed"
    assert done["error_code"] is None
    assert not any(
        item["type"] == "error" and item.get("code") == "tts_backpressure" for item in events
    )
    turn = next(iter(repo.records.values()))
    assert len(turn.segments) == len(completed_segments)
    assert "".join(segment.text for segment in turn.segments) == response
    assert all(segment.complete and segment.samples > 0 for segment in turn.segments)


@pytest.mark.parametrize("enable_thinking", [False, True])
def test_audio_starts_while_model_is_still_generating_and_reports_latency(
    voice_env, monkeypatch, enable_thinking
):
    import app.api.voice as voice_routes
    import app.api.voice_connection as voice_session
    from app.services.agent_runner import AgentEvent

    client, _, _, headers = voice_env
    signals = []
    overlapped = []

    class SignalingSpeech(FakeSpeech):
        def __init__(self, config):
            super().__init__(config)
            self.emitted = asyncio.Event()
            signals.append(self.emitted)

        async def synthesize(self, ws, text):
            yield b"\0\0" * 480
            self.emitted.set()

    async def stalled_graph(*args, **kwargs):
        assert args[1]["config"]["configurable"]["voice_enable_thinking"] is enable_thinking
        yield AgentEvent("token", "我在认真听", "message-1")
        try:
            await asyncio.wait_for(signals[0].wait(), timeout=1)
        except TimeoutError:
            overlapped.append(False)
        else:
            overlapped.append(True)
        yield AgentEvent("token", "，你慢慢说。", "message-1")

    monkeypatch.setattr(voice_routes, "AlibabaRealtime", SignalingSpeech)
    monkeypatch.setattr(settings, "VOICE_LLM_ENABLE_THINKING", enable_thinking)
    monkeypatch.setattr(voice_session, "graph_events", stalled_graph)
    auth = headers()
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="测试边生成边播报")
        events = read_until(ws, "response.done")
        event(ws, "session.close")
        read_until(ws, "session.closed")

    assert overlapped == [True]
    first_audio = next(index for index, item in enumerate(events) if item["type"] == "audio")
    last_text = max(index for index, item in enumerate(events) if item["type"] == "text.delta")
    assert first_audio < last_text
    assert (
        "".join(item["text"] for item in events if item["type"] == "text.delta")
        == "我在认真听，你慢慢说。"
    )
    latency = events[-1]["latency_ms"]
    assert 0 <= latency["first_text_ms"] <= latency["first_speech_ms"]
    assert latency["first_speech_ms"] <= latency["first_audio_ms"] <= latency["first_audio_sent_ms"]
    assert latency["text_to_audio_ms"] >= 0


@pytest.mark.parametrize("failure", ["before_audio", "after_audio", "persistent"])
@pytest.mark.parametrize("error_type", [TimeoutError, SpeechProviderError])
def test_tts_failure_retries_only_undelivered_segments(voice_env, monkeypatch, failure, error_type):
    import app.api.voice as voice_routes

    connections = []
    attempts = []

    class TimeoutSpeech(FakeSpeech):
        @asynccontextmanager
        async def synthesizer(self, session):
            connection = object()
            connections.append(connection)
            yield connection

        async def synthesize(self, ws, text):
            attempts.append((ws, text))
            if len(attempts) == 2 or (failure == "persistent" and len(attempts) > 2):
                if failure == "after_audio":
                    yield b"\0\0" * 480
                raise error_type("upstream stalled")
            yield b"\0\0" * 480

    client, repo, agents, headers = voice_env
    response = "第一句正常播放。第二句测试超时。第三句继续播放。"
    agents["chatbot"] = build_chatbot([response])
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", TimeoutSpeech)
    auth = headers()
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="测试分段超时恢复")
        events = read_until(ws, "response.done")
        event(ws, "session.close")
        read_until(ws, "session.closed")

    assert "".join(item["text"] for item in events if item["type"] == "text.delta") == response
    turn = next(iter(repo.records.values()))
    assert turn.execution_status == "completed"
    if failure == "before_audio":
        assert events[-1]["audio_status"] == "completed"
        assert "".join(segment.text for segment in turn.segments) == response
        assert all(segment.complete and segment.samples == 480 for segment in turn.segments)
        assert len(connections) == 2
        assert attempts[1][1] == attempts[2][1]
        assert attempts[1][0] is not attempts[2][0]
        assert not any(item["type"] == "error" for item in events)
    else:
        assert events[-1]["audio_status"] == "failed"
        assert events[-1]["error_code"] == "tts_failed"
        assert len(connections) == (1 if failure == "after_audio" else 3)
        assert len(attempts) == (2 if failure == "after_audio" else 4)
        assert turn.segments[0].complete
        assert not turn.segments[1].complete
        assert turn.segments[1].samples == (480 if failure == "after_audio" else 0)


def test_tts_failure_finishes_without_deadlock_or_false_success(voice_env, monkeypatch):
    import app.api.voice as voice_routes

    client, repo, agents, headers = voice_env
    agents["chatbot"] = build_chatbot(["".join(f"这是第{index}句。" for index in range(1, 21))])
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", FailingSpeech)
    auth = headers()
    session = create(client, auth)

    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="测试语音失败")
        events = read_until(ws, "response.done")
        event(ws, "session.close")
        read_until(ws, "session.closed")

    done = events[-1]
    error = next(item for item in events if item["type"] == "error")
    assert error["response_id"] == done["response_id"]
    assert error["code"] == "tts_failed"
    assert done["status"] == "failed"
    assert done["execution_status"] == "completed"
    assert done["audio_status"] == "failed"
    assert done["error_code"] == "tts_failed"
    turn = next(iter(repo.records.values()))
    assert turn.status == "failed"
    assert turn.execution_status == "completed"
    assert turn.audio_status == "failed"


def test_standalone_punctuation_does_not_truncate_the_rest_of_a_reply(voice_env, monkeypatch):
    import app.api.voice as voice_routes
    import app.api.voice_connection as voice_session
    from app.services.agent_runner import AgentEvent

    first_audio = asyncio.Event()

    class ValidTextSpeech(FakeSpeech):
        async def synthesize(self, ws, text):
            if not any(char.isalnum() for char in text):
                raise SpeechProviderError("Speech inference task failed")
            yield b"\0\0" * 480
            first_audio.set()

    async def stalled_graph(*args, **kwargs):
        yield AgentEvent("token", "我说你好。", "message-1")
        await asyncio.wait_for(first_audio.wait(), 1)
        yield AgentEvent("token", "”", "message-1")
        await asyncio.sleep(0.45)
        yield AgentEvent("token", "接下来还有完整的回答。", "message-1")

    client, repo, agents, headers = voice_env
    response = "我说你好。”接下来还有完整的回答。"
    agents["chatbot"] = build_chatbot([response], sleep=0.01)
    monkeypatch.setattr(voice_session, "graph_events", stalled_graph)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", ValidTextSpeech)
    auth = headers()
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="检查引号后继续播报")
        events = read_until(ws, "response.done")
        event(ws, "session.close")
        read_until(ws, "session.closed")

    assert events[-1]["audio_status"] == "completed"
    turn = next(iter(repo.records.values()))
    assert "".join(char for char in turn.generated_text if char.isalnum()) == "".join(
        char for segment in turn.segments for char in segment.text if char.isalnum()
    )
    assert all(segment.complete and segment.samples > 0 for segment in turn.segments)


@pytest.mark.parametrize("source", ["voices", "voice_catalog"])
@pytest.mark.parametrize("lingxin_first", [True, False])
def test_nacos_voice_order_controls_capabilities_and_session_default(
    voice_env, monkeypatch, source: str, lingxin_first: bool
) -> None:
    from app.core.nacos import apply_remote_settings
    from app.core.settings import Settings

    client, _, _, headers = voice_env
    model = "qwen-audio-3.1-tts-flash"
    options = [
        VoiceOption(id="longanlingxin_v3.1", name="龙安灵心", description="多语言与方言女声"),
        VoiceOption(id="longanhuan_v3.1", name="龙安欢", description="多语言与方言女声"),
    ]
    if not lingxin_first:
        options.reverse()
    serialized_options = [option.model_dump() for option in options]
    realtime = {"tts_model": model, "voices_model": model, "voices": serialized_options}
    if source == "voice_catalog":
        realtime["voices"] = list(reversed(serialized_options))
        realtime["voice_catalog"] = {model: serialized_options}
    config = Settings(_env_file=None, USE_FAKE_MODEL=True)
    updated = apply_remote_settings(
        config, json.dumps({"voice": {"enabled": True, "realtime": realtime}})
    )
    for name in updated:
        monkeypatch.setattr(settings, name, getattr(config, name))
    auth = headers()

    response = client.get("/voice/capabilities", headers=auth)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["voices"] == [option.id for option in options]
    assert data["voice_options"] == serialized_options
    assert data["default_voice"] == options[0].id
    assert data["voice_catalog_model"] == model
    assert create(client, auth)["voice"] == options[0].id


def test_voice_capabilities_expose_compatible_ids_and_display_metadata(voice_env):
    client, _, _, headers = voice_env
    auth = headers()

    response = client.get("/voice/capabilities", headers=auth)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["voices"] == ["Cherry", "Serena"]
    assert data["default_voice"] == "Cherry"
    assert data["voice_options"] == [
        {
            "id": "Cherry",
            "name": "芊悦",
            "description": "阳光积极、亲切自然",
            "languages": ["zh", "en"],
        },
        {"id": "Serena", "name": "苏瑶", "description": "温柔自然", "languages": ["zh", "en"]},
    ]
    assert data["wake_word"]["enabled"] is True
    assert data["wake_word"]["keyword"] == "小美小美"
    assert data["client_vad"]["enabled"] is True
    assert data["audio_metrics_seconds"] > 0


@pytest.mark.parametrize(
    "transcript",
    [
        "小美小美介绍一下你自己",
        "小美，小美，介绍一下你自己",
        "小 美！小 美！小美小美，介绍一下你自己",
    ],
)
def test_wake_session_validates_metadata_and_confirms_transcript(
    voice_env, monkeypatch, transcript
):
    import app.api.voice as voice_routes

    client, repo, _, headers = voice_env
    monkeypatch.setattr(WakeSpeech, "transcript", transcript)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", WakeSpeech)
    auth = headers()
    rejected = client.post(
        "/voice/sessions",
        headers=auth,
        json={"activation": "wake_word", "wake_word": "错误唤醒词"},
    )
    assert rejected.status_code == 422

    session = create(
        client,
        auth,
        activation="wake_word",
        wake_word="小美",
        wake_engine="sherpa-onnx-1.13.8",
        pre_roll_samples=16000,
    )
    assert session["wake_status"] == "pending"
    assert session["wake_word"] == "小美小美"
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        frame = encode_audio(
            b"\0\0" * 320,
            ready["connection_id"],
            str(ZERO_UUID),
            0,
            0,
            kind=1,
        )
        ws.send_bytes(frame)
        events = read_until(ws, "response.done")
        event(ws, "session.close")
        read_until(ws, "session.closed")

    assert any(item["type"] == "wake.accepted" for item in events)
    assert (
        next(item for item in events if item["type"] == "wake.accepted")["command"]
        == "介绍一下你自己"
    )
    output = "".join(item["text"] for item in events if item["type"] == "text.delta")
    assert output.startswith(settings.VOICE_WAKE_GREETING)
    turn = next(iter(repo.records.values()))
    assert turn.input_text == "介绍一下你自己"
    assert repo.sessions[session["session_id"]].wake_status == "accepted"


@pytest.mark.parametrize("transcript", ["小美小美", "小美，小美，小美！", "小美，小美，嗯"])
def test_wake_without_command_only_speaks_greeting(voice_env, monkeypatch, transcript):
    import app.api.voice as voice_routes

    client, repo, _, headers = voice_env
    monkeypatch.setattr(WakeSpeech, "transcript", transcript)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", WakeSpeech)
    auth = headers()
    session = create(
        client,
        auth,
        activation="wake_word",
        wake_word="小美",
        wake_engine="sherpa-onnx-1.13.8",
    )
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        ws.send_bytes(
            encode_audio(
                b"\0\0" * 320,
                ready["connection_id"],
                str(ZERO_UUID),
                0,
                0,
                kind=1,
            )
        )
        events = read_until(ws, "response.done")
        assert sum(item["type"] == "wake.accepted" for item in events) == 1
        assert any(item["type"] == "audio" for item in events)
        assert (
            "".join(item["text"] for item in events if item["type"] == "text.delta")
            == settings.VOICE_WAKE_GREETING
        )
        event(ws, "playback.finished", response_id=events[-1]["response_id"])
        event(ws, "ping")
        read_until(ws, "pong")
        event(ws, "session.close")
        read_until(ws, "session.closed")

    assert len(repo.records) == 1
    turn = next(iter(repo.records.values()))
    assert turn.input_text == ""
    assert turn.run_id is None
    assert turn.audio_status == "completed"
    assert repo.sessions[session["session_id"]].wake_status == "accepted"


def test_split_wake_and_extra_calls_greet_once_and_preserve_following_command(
    voice_env, monkeypatch
):
    import app.api.voice as voice_routes

    client, repo, _, headers = voice_env
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", SegmentedWakeSpeech)
    auth = headers()
    session = create(client, auth, activation="wake_word", wake_word="小美，小美")
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        all_events = []
        for sequence in range(4):
            ws.send_bytes(
                encode_audio(
                    b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, sequence, kind=1
                )
            )
            events = read_until(ws, "response.done" if sequence in {1, 3} else "transcript.final")
            event(ws, "ping")
            events.extend(read_until(ws, "pong"))
            all_events.extend(events)
            if sequence == 0:
                assert not any(e["type"] == "wake.accepted" for e in events)
                assert repo.records == {}
            if sequence == 2:
                assert next(e for e in events if e["type"] == "transcript.final")["text"] == ""
                assert not any(
                    e["type"] in {"response.cancelled", "input.accepted"} for e in events
                )
        event(ws, "session.close")
        read_until(ws, "session.closed")

    assert sum(e["type"] == "wake.accepted" for e in all_events) == 1
    assert (
        sum(
            e["type"] == "text.delta" and e.get("text") == settings.VOICE_WAKE_GREETING
            for e in all_events
        )
        == 1
    )
    assert [turn.input_text for turn in repo.records.values()] == ["", "介绍一下你自己"]


@pytest.mark.parametrize("transcript", [None, "小美"])
def test_wake_confirmation_timeout_closes_without_greeting(voice_env, monkeypatch, transcript):
    import app.api.voice as voice_routes

    client, repo, _, headers = voice_env
    monkeypatch.setattr(settings, "VOICE_WAKE_CONFIRM_SECONDS", 2)
    monkeypatch.setattr(WakeSpeech, "transcript", transcript)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", WakeSpeech)
    auth = headers()
    session = create(client, auth, activation="wake_word", wake_word="小美小美")
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        if transcript is not None:
            ws.send_bytes(
                encode_audio(b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, 0, kind=1)
            )
        events = read_until(ws, "wake.rejected")
    assert not any(e["type"] in {"wake.accepted", "response.started", "audio"} for e in events)
    assert repo.records == {}


def test_wake_greeting_tts_failure_keeps_session_usable(voice_env, monkeypatch):
    import app.api.voice as voice_routes

    client, repo, _, headers = voice_env
    monkeypatch.setattr(WakeSpeech, "transcript", "小美小美")
    monkeypatch.setattr(WakeSpeech, "synthesize", FailingSpeech.synthesize)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", WakeSpeech)
    auth = headers()
    session = create(client, auth, activation="wake_word", wake_word="小美小美")
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        ws.send_bytes(
            encode_audio(b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, 0, kind=1)
        )
        events = read_until(ws, "response.done")
        assert events[-1]["audio_status"] == "failed"
        assert any(e.get("code") == "tts_failed" for e in events)
        event(ws, "ping")
        read_until(ws, "pong")
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert next(iter(repo.records.values())).execution_status == "completed"


@pytest.mark.parametrize(
    "filler",
    ["嗯", "呢", "那", "。", ".", "...", "．", "，？！", "；：", "——", "“”", "（ ）", "嗯。"],
)
def test_playback_echo_filler_does_not_cancel_or_start_a_turn(voice_env, monkeypatch, filler):
    import app.api.voice as voice_routes

    client, repo, _, headers = voice_env
    monkeypatch.setattr(EchoFillerSpeech, "second_transcript", filler)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", EchoFillerSpeech)
    auth = headers()
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        for sequence in range(2):
            ws.send_bytes(
                encode_audio(
                    b"\0\0" * 320,
                    ready["connection_id"],
                    str(ZERO_UUID),
                    0,
                    sequence,
                    kind=1,
                )
            )
            events = read_until(ws, "response.done" if sequence == 0 else "transcript.final")
            if sequence == 0:
                response_id = events[-1]["response_id"]
            else:
                assert events[-1]["text"] == ""
                assert not any(item["type"] == "response.cancelled" for item in events)
        event(ws, "ping")
        events = read_until(ws, "pong")
        assert not any(item["type"] == "response.cancelled" for item in events)
        event(ws, "playback.finished", response_id=response_id)
        event(ws, "ping")
        read_until(ws, "pong")
        event(ws, "session.close")
        read_until(ws, "session.closed")

    assert len(repo.records) == 1
    assert next(iter(repo.records.values())).response_id == response_id
    assert not next(iter(repo.records.values())).interrupted


@pytest.mark.parametrize("text", ["你好。", "C++ 的 1.5 版本怎么用？", "嗯，帮我查天气。"])
def test_punctuation_noise_is_ignored_before_meaningful_speech(voice_env, monkeypatch, text):
    import app.api.voice as voice_routes

    class PartialSpeech(SegmentedWakeSpeech):
        async def append_audio(self, ws, pcm):
            transcript = self.transcripts[self.index]
            await self.events.put(
                {
                    "type": "conversation.item.input_audio_transcription.text",
                    "text": transcript[:-1],
                    "stash": transcript[-1:],
                }
            )
            await super().append_audio(ws, pcm)

    client, repo, _, headers = voice_env
    monkeypatch.setattr(SegmentedWakeSpeech, "transcripts", ("。", text))
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", PartialSpeech)
    auth = headers()
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        for sequence in range(2):
            ws.send_bytes(
                encode_audio(
                    b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, sequence, kind=1
                )
            )
            events = read_until(ws, "transcript.final" if sequence == 0 else "response.done")
            if sequence == 0:
                assert events[-1]["text"] == ""
                assert next(e for e in events if e["type"] == "transcript.partial")["text"] == ""
                event(ws, "ping")
                events.extend(read_until(ws, "pong"))
                assert not any(e["type"] in {"input.accepted", "response.started"} for e in events)
                assert repo.records == {}
            else:
                assert next(e for e in events if e["type"] == "transcript.partial")["text"] == text
                event(ws, "playback.finished", response_id=events[-1]["response_id"])
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert [turn.input_text for turn in repo.records.values()] == [text]


def test_punctuation_noise_does_not_reject_or_erase_a_split_wake(voice_env, monkeypatch):
    import app.api.voice as voice_routes

    client, repo, _, headers = voice_env
    monkeypatch.setattr(SegmentedWakeSpeech, "transcripts", (".", "小美", "。", "小美"))
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", SegmentedWakeSpeech)
    auth = headers()
    session = create(client, auth, activation="wake_word", wake_word="小美小美")
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        all_events = []
        for sequence in range(4):
            ws.send_bytes(
                encode_audio(
                    b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, sequence, kind=1
                )
            )
            events = read_until(ws, "response.done" if sequence == 3 else "transcript.final")
            event(ws, "ping")
            events.extend(read_until(ws, "pong"))
            all_events.extend(events)
            if sequence < 3:
                assert not any(e["type"] in {"wake.accepted", "response.started"} for e in events)
                assert repo.records == {}
                if sequence in {0, 2}:
                    assert next(e for e in events if e["type"] == "transcript.final")["text"] == ""
        accepted = [e for e in all_events if e["type"] == "wake.accepted"]
        assert len(accepted) == 1
        assert accepted[0]["command"] == ""
        response = next(e for e in all_events if e["type"] == "response.done")
        event(ws, "playback.finished", response_id=response["response_id"])
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert [turn.input_text for turn in repo.records.values()] == [""]
    assert repo.sessions[session["session_id"]].wake_status == "accepted"


def test_meaningful_speech_still_interrupts_playback(voice_env, monkeypatch):
    import app.api.voice as voice_routes

    client, repo, _, headers = voice_env
    monkeypatch.setattr(EchoFillerSpeech, "second_transcript", "请暂停播放")
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", EchoFillerSpeech)
    auth = headers()
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        for sequence in range(2):
            ws.send_bytes(
                encode_audio(
                    b"\0\0" * 320,
                    ready["connection_id"],
                    str(ZERO_UUID),
                    0,
                    sequence,
                    kind=1,
                )
            )
            events = read_until(ws, "response.done")
            if sequence == 0:
                first_response_id = events[-1]["response_id"]
            else:
                assert any(
                    item["type"] == "response.cancelled"
                    and item["response_id"] == first_response_id
                    for item in events
                )
        event(ws, "session.close")
        read_until(ws, "session.closed")

    assert len(repo.records) == 2
    assert any(turn.input_text == "请暂停播放" for turn in repo.records.values())
    assert any(
        turn.response_id == first_response_id and turn.interrupted for turn in repo.records.values()
    )


def test_rejected_wake_closes_session_and_releases_user_capacity(voice_env, monkeypatch):
    import app.api.voice as voice_routes

    client, repo, _, headers = voice_env
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", RejectedWakeSpeech)
    auth = headers()
    session = create(
        client,
        auth,
        activation="wake_word",
        wake_word="小美",
        wake_engine="sherpa-onnx-1.13.8",
        pre_roll_samples=16000,
    )

    with connect(client, session, auth) as ws:
        ready = configure(ws)
        ws.send_bytes(
            encode_audio(
                b"\0\0" * 320,
                ready["connection_id"],
                str(ZERO_UUID),
                0,
                0,
                kind=1,
            )
        )
        events = read_until(ws, "wake.rejected")

    assert any(item["type"] == "wake.rejected" for item in events)
    deadline = monotonic() + 2
    while repo.sessions[session["session_id"]].status != "closed" and monotonic() < deadline:
        sleep(0.01)
    rejected = repo.sessions[session["session_id"]]
    assert rejected.wake_status == "rejected"
    assert rejected.status == "closed"
    assert asyncio.run(repo.active_sessions(rejected.user_id)) == 0


def test_audio_metrics_are_aggregated_on_session(voice_env):
    client, repo, _, headers = voice_env
    auth = headers()
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        configure(ws)
        event(
            ws,
            "audio.metrics",
            frames=125,
            rms_dbfs=-28.5,
            peak_dbfs=-4.0,
            clipped_samples=2,
            aec_enabled=True,
            noise_suppression_enabled=True,
            mode="conversation",
        )
        event(ws, "session.close")
        read_until(ws, "session.closed")

    quality = repo.sessions[session["session_id"]].audio_quality
    assert quality.reports == 1
    assert quality.frames == 125
    assert quality.average_rms_dbfs == -28.5
    assert quality.peak_dbfs == -4.0
    assert quality.clipped_samples == 2
    assert quality.aec_enabled is True
    assert quality.noise_suppression_enabled is True


def test_voice_session_accepts_configured_voice_and_rejects_unknown_voice(voice_env):
    client, _, _, headers = voice_env
    auth = headers()

    selected = create(client, auth, voice="Serena")
    rejected = client.post("/voice/sessions", headers=auth, json={"voice": "Unknown"})

    assert selected["voice"] == "Serena"
    assert rejected.status_code == 422
    assert rejected.json()["message"] == "Unsupported voice"


def test_voice_session_fails_cleanly_when_no_voice_is_configured(voice_env, monkeypatch):
    client, _, _, headers = voice_env
    auth = headers()
    monkeypatch.setattr(settings, "VOICE_REALTIME_VOICES", [])

    response = client.post("/voice/sessions", headers=auth, json={})

    assert response.status_code == 503
    assert response.json()["message"] == "No realtime voices are configured"


def test_tokens_bind_user_and_protect_every_thread_entry(voice_env):
    client, repo, agents, headers = voice_env
    alice, bob = headers(), headers("bob")
    session = create(client, alice)
    assert client.post("/voice/sessions", headers=alice, json={"user_id": "bob"}).status_code == 403
    assert client.get(f"/voice/sessions/{session['session_id']}", headers=bob).status_code == 403
    for route in ("history", "invoke", "stream"):
        body = {"thread_id": session["thread_id"]}
        if route != "history":
            body["message"] = "steal"
        assert client.post(f"/chatbot/{route}", headers=bob, json=body).status_code == 403
    assert (
        client.get("/chatbot/threads", params={"user_id": "alice"}, headers=bob).status_code == 403
    )
    assert client.post("/auth/token", headers=bob, json={"user_id": "alice"}).status_code == 403
    run = client.post("/chatbot/invoke", headers=alice, json={"message": "hello"}).json()["data"]
    assert (
        client.post(
            "/feedback",
            headers=bob,
            json={"run_id": run["run_id"], "key": "rating", "score": 1},
        ).status_code
        == 403
    )
    with pytest.raises(WebSocketDisconnect) as caught:
        with connect(client, session, bob):
            pass
    assert caught.value.code == 4403
    response = client.post(
        "/agui/chatbot/run",
        headers=bob,
        json={
            "threadId": session["thread_id"],
            "runId": "r",
            "messages": [],
            "tools": [],
            "context": [],
            "state": {},
            "forwardedProps": {},
        },
    )
    assert response.status_code == 403


@pytest.mark.parametrize("input_mode", ["conversation", "text"])
def test_cancel_invalidates_audio_and_recovers_functional_history(voice_env, input_mode):
    client, repo, agents, headers = voice_env
    agents["chatbot"] = build_chatbot(["第一句话。" + "后续内容" * 100, "新的回答。"], sleep=0.003)
    auth = headers()
    session = create(client, auth, input_mode=input_mode)
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="第一个问题")
        first = read_until(ws, "audio.segment.done")
        old_id = first[-1]["response_id"]
        event(
            ws,
            "playback.progress",
            response_id=old_id,
            segment_index=1,
            played_samples=first[-1]["samples"],
        )
        event(ws, "response.cancel", response_id=old_id)
        read_until(ws, "response.cancelled")
        event(ws, "input.text", text="第二个问题")
        seen = read_until(ws, "response.done")
        if seen[-1]["response_id"] == old_id:
            seen += read_until(ws, "response.done")
        assert all(e["frame"][2] != old_id for e in seen if e["type"] == "audio")
        event(ws, "session.close")
        read_until(ws, "session.closed")
    turns = list(repo.records.values())
    assert turns[0].interrupted and turns[0].execution_status == "cancelled"
    assert turns[0].audio_status == "cancelled"
    history = client.post(
        "/chatbot/history", headers=auth, json={"thread_id": session["thread_id"]}
    ).json()["data"]
    contents = [m["content"] for m in history["messages"]]
    assert "第一个问题" in contents and "第二个问题" in contents
    assert any("第一句话。" in c and "语音播放记录" in c for c in contents)


@pytest.mark.parametrize("input_mode", ["conversation", "text"])
def test_reconnect_deduplicates_text_and_rejects_second_socket(voice_env, input_mode):
    client, repo, agents, headers = voice_env
    auth = headers()
    session = create(client, auth, input_mode=input_mode)
    message = {"type": "input.text", "event_id": "stable-input", "text": "只执行一次"}
    with connect(client, session, auth) as ws:
        configure(ws)
        with pytest.raises(WebSocketDisconnect) as caught:
            with connect(client, session, auth):
                pass
        assert caught.value.code == 4409
        ws.send_json(message)
        read_until(ws, "response.done")
    with connect(client, session, auth) as ws:
        configure(ws)
        ws.send_json(message)
        assert read_until(ws, "input.accepted")[-1]["duplicate"]
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert len(repo.records) == 1


@pytest.mark.parametrize("input_mode", ["conversation", "text"])
def test_business_interrupt_requires_explicit_approval(voice_env, input_mode):
    client, repo, agents, headers = voice_env

    async def ask(state):
        answer = interrupt("确认执行？")
        return {"messages": [AIMessage(content=f"已确认：{answer}")]}

    graph = StateGraph(MessagesState)
    graph.add_node("ask", ask)
    graph.add_edge("__start__", "ask")
    graph.add_edge("ask", END)
    agents["approval"] = graph.compile(checkpointer=MemorySaver())
    auth = headers()
    session = create(client, auth, agent_id="approval", input_mode=input_mode)
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="开始")
        events = read_until(ws, "response.done")
        required = next(e for e in events if e["type"] == "approval.required")
        event(ws, "input.text", text="这不是确认")
        assert read_until(ws, "error")[-1]["code"] == "approval_required"
        event(ws, "approval.submit", interrupt_id=required["interrupt_id"], value="同意")
        events = read_until(ws, "response.done")
        assert "同意" in "".join(e["text"] for e in events if e["type"] == "text.delta")
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert len(repo.records) == 2


@pytest.mark.parametrize("bad_frame", [b"bad", b"VCE1" + b"\0" * 44])
def test_invalid_binary_closes_session(voice_env, bad_frame):
    client, repo, agents, headers = voice_env
    auth = headers()
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        configure(ws)
        ws.send_bytes(bad_frame)
        assert read_until(ws, "session.closed")[-1]["reason"] == "invalid_protocol"


def test_expired_session_cannot_connect(voice_env):
    client, repo, agents, headers = voice_env
    auth = headers()
    session = create(client, auth)
    repo.sessions[session["session_id"]].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(WebSocketDisconnect) as caught:
        with connect(client, session, auth):
            pass
    assert caught.value.code == 4410


@pytest.mark.asyncio
async def test_voice_lifespan_requires_both_backend_secrets(monkeypatch):
    from app.lifecycle import voice_lifespan

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("admin"))
    monkeypatch.setattr(settings, "APP_TOKEN_SECRET", None)
    with pytest.raises(ValueError, match="APP_TOKEN_SECRET"):
        async with voice_lifespan(FastAPI()):
            pass

    monkeypatch.setattr(settings, "AUTH_SECRET", None)
    monkeypatch.setattr(settings, "APP_TOKEN_SECRET", SecretStr("a" * 48))
    with pytest.raises(ValueError, match="AUTH_SECRET"):
        async with voice_lifespan(FastAPI()):
            pass


@pytest.mark.parametrize("agent_id", ["mem0-chatbot", "chatbot"])
def test_mem0_agent_shares_text_and_voice_memory(voice_env, monkeypatch, agent_id):
    import importlib
    from unittest.mock import AsyncMock, Mock

    import app.agents.runtime as runtime
    import app.api.agui as agui
    import app.api.chats as service
    import app.api.voice as voice_routes
    from app.api.dependencies import get_agent

    module = importlib.import_module("app.agents.mem0_chat")
    client, repo, agents, headers = voice_env
    memory = Mock(search=AsyncMock(return_value=["喜欢茶"]), remember=AsyncMock())
    monkeypatch.setattr(module, "get_mem0_service", lambda: memory)
    monkeypatch.setattr(
        module,
        "get_model",
        lambda _, **kwargs: FakeListChatModel(
            responses=["记得你喜欢茶。", "记得你喜欢茶。"], sleep=0.001
        ),
    )
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    monkeypatch.setattr(runtime, "mem0_chat", graph)
    for route_module in (service, voice_routes, agui):
        monkeypatch.setattr(route_module, "get_agent", get_agent)
    auth = headers()
    response = client.post(f"/{agent_id}/invoke", headers=auth, json={"message": "我喜欢茶"})
    assert response.status_code == 200
    session = create(client, auth, **({"agent_id": agent_id} if agent_id != "chatbot" else {}))
    assert session["agent_id"] == agent_id
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="我喜欢什么？")
        seen = read_until(ws, "response.done")
        assert "".join(e["text"] for e in seen if e["type"] == "text.delta") == "记得你喜欢茶。"
        assert any(e["type"] == "audio" for e in seen)
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert [call.args[1] for call in memory.search.await_args_list] == ["alice", "alice"]
    assert [call.args[0] for call in memory.remember.await_args_list] == [
        [{"role": "user", "content": "我喜欢茶"}],
        [{"role": "user", "content": "我喜欢什么？"}],
    ]
    capabilities = client.get("/voice/capabilities", headers=auth).json()["data"]
    agent = next(a for a in capabilities["agents"] if a["id"] == "mem0-chatbot")
    assert agent["speech_mode"] == "streaming" and agent["cancel_execution"]
    other_agent = "chatbot" if agent_id == "mem0-chatbot" else "mem0-chatbot"
    assert (
        client.post(
            f"/{other_agent}/history", headers=auth, json={"thread_id": session["thread_id"]}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("agent_id", ["mem0-chatbot", "chatbot"])
def test_mem0_cancel_invalidates_audio(voice_env, agent_id):
    client, repo, agents, headers = voice_env
    agents[agent_id] = build_chatbot(["第一句话。" + "后续内容" * 100, "新的回答。"], sleep=0.003)
    auth = headers()
    session = create(client, auth, agent_id=agent_id)
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="第一个问题")
        first = read_until(ws, "audio.segment.done")
        old_id = first[-1]["response_id"]
        event(ws, "response.cancel", response_id=old_id)
        read_until(ws, "response.cancelled")
        event(ws, "input.text", text="第二个问题")
        seen = read_until(ws, "response.done")
        if seen[-1]["response_id"] == old_id:
            seen += read_until(ws, "response.done")
        assert all(e["frame"][2] != old_id for e in seen if e["type"] == "audio")
        event(ws, "session.close")
        read_until(ws, "session.closed")
    turns = list(repo.records.values())
    assert turns[0].interrupted and turns[0].execution_status == "cancelled"


@pytest.mark.parametrize(
    "action,confirmation,status",
    [
        ("update", "确认修改", "updated"),
        ("delete", "确认删除", "deleted"),
        ("delete", "取消", "cancelled"),
        ("delete", "确认删除", "operation_failed"),
    ],
)
def test_audio_transcripts_manage_memories_and_speak_results(
    voice_env, monkeypatch, action, confirmation, status
):
    import importlib

    import app.api.voice as voice_routes
    from app.schemas.memory import MemoryList, MemoryRecord
    from app.schemas.memory_commands import MemoryMutationResult
    from app.services.memory.mem0_service import Mem0Service

    module = importlib.import_module("app.agents.mem0_chat")
    client, repo, agents, headers = voice_env
    command = "把喜欢茶的记忆改成我喜欢咖啡" if action == "update" else "删除喜欢茶的记忆"

    class CommandSpeech(FakeSpeech):
        def __init__(self, config):
            super().__init__(config)
            self.utterances = 0

        async def append_audio(self, ws, pcm):
            self.utterances += 1
            item_id = f"speech-{self.utterances}"
            text = command if self.utterances == 1 else confirmation
            await self.events.put({"type": "input_audio_buffer.speech_started", "item_id": item_id})
            for _ in range(2):
                await self.events.put(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": item_id,
                        "transcript": text,
                    }
                )

    class PlanningModel(FakeListChatModel):
        def with_structured_output(self, schema, **kwargs):
            return self | RunnableLambda(
                lambda message: schema.model_validate_json(message.content)
            )

    record = MemoryRecord(id=uuid4(), memory="我喜欢茶", user_id="alice", agent_id="mem0-chatbot")
    memory = AsyncMock(spec=Mem0Service)
    memory.search_memory_records.return_value = MemoryList(memories=[record])
    memory.apply_memory_mutation.return_value = MemoryMutationResult(
        status="updated" if action == "update" else "deleted", deleted_count=int(action == "delete")
    )
    if status == "operation_failed":
        memory.apply_memory_mutation.side_effect = TimeoutError("private-text")
    model = PlanningModel(
        responses=[
            json.dumps(
                {
                    "action": action,
                    "query": "喜欢茶",
                    "text": "我喜欢咖啡" if action == "update" else None,
                }
            ),
            '{"matching_indices":[1]}',
        ]
    )
    monkeypatch.setattr(module, "get_mem0_service", lambda: memory)
    monkeypatch.setattr(module, "get_model", lambda _, **kwargs: model)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", CommandSpeech)
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    agents["mem0-chatbot"] = graph
    auth = headers()
    session = create(client, auth, agent_id="mem0-chatbot")
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        ws.send_bytes(
            encode_audio(b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, 0, kind=1)
        )
        preview = read_until(ws, "response.done")
        spoken = "".join(item["text"] for item in preview if item["type"] == "text.delta")
        assert "我喜欢茶" in spoken and "请说确认" in spoken
        assert "matching_indices" not in spoken and '"action"' not in spoken
        assert any(item["type"] == "audio" for item in preview)
        memory.apply_memory_mutation.assert_not_awaited()
        event(ws, "playback.finished", response_id=preview[-1]["response_id"])
        ws.send_bytes(
            encode_audio(b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, 1, kind=1)
        )
        result = read_until(ws, "response.done")
        spoken = "".join(item["text"] for item in result if item["type"] == "text.delta")
        assert any(item["type"] == "audio" for item in result)
        if status == "updated":
            assert "已更新" in spoken and "我喜欢咖啡" in spoken
        elif status == "deleted":
            assert "已删除" in spoken
        elif status == "cancelled":
            assert "已取消" in spoken
        else:
            assert "未能确认完成" in spoken and "已删除" not in spoken
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert len(repo.records) == 2
    if status == "cancelled":
        memory.apply_memory_mutation.assert_not_awaited()
    else:
        memory.apply_memory_mutation.assert_awaited_once()
        user_id, mutation = memory.apply_memory_mutation.await_args.args
        assert user_id == "alice" and mutation.target.id == record.id
    memory.remember.assert_not_awaited()


def test_cancelled_memory_preview_does_not_mutate_or_stream_planning_json(voice_env, monkeypatch):
    import importlib

    from app.services.memory.mem0_service import Mem0Service

    module = importlib.import_module("app.agents.mem0_chat")
    client, _, agents, headers = voice_env

    class SlowPlanningModel(FakeListChatModel):
        def with_structured_output(self, schema, **kwargs):
            return self | RunnableLambda(
                lambda message: schema.model_validate_json(message.content)
            )

    model = SlowPlanningModel(responses=['{"action":"delete","query":"tea"}'], sleep=0.03)
    memory = AsyncMock(spec=Mem0Service)
    monkeypatch.setattr(module, "get_mem0_service", lambda: memory)
    monkeypatch.setattr(module, "get_model", lambda _, **kwargs: model)
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    agents["mem0-chatbot"] = graph
    auth = headers()
    session = create(client, auth, agent_id="mem0-chatbot")
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="删除喜欢茶的记忆")
        started = read_until(ws, "response.started")
        old_id = started[-1]["response_id"]
        event(ws, "response.cancel", response_id=old_id)
        read_until(ws, "response.cancelled")
        event(ws, "input.text", text="确认删除")
        seen = read_until(ws, "response.done")
        if seen[-1]["response_id"] == old_id:
            seen += read_until(ws, "response.done")
        text = "".join(item["text"] for item in seen if item["type"] == "text.delta")
        assert "当前没有等待确认" in text
        assert '"action"' not in text
        assert all(item["frame"][2] != old_id for item in seen if item["type"] == "audio")
        event(ws, "session.close")
        read_until(ws, "session.closed")
    memory.apply_memory_mutation.assert_not_awaited()


@pytest.mark.parametrize("operation", ["count", "lookup", "delete_many", "delete_all"])
def test_audio_memory_queries_and_bulk_deletion(voice_env, monkeypatch, operation):
    import importlib

    import app.api.voice as voice_routes
    from app.schemas.memory import MemoryList, MemoryRecord
    from app.schemas.memory_commands import MemoryMutationResult, MemorySnapshot
    from app.services.memory.mem0_service import Mem0Service

    module = importlib.import_module("app.agents.mem0_chat")
    client, repo, agents, headers = voice_env
    scenarios = {
        "count": (["我的记忆总共有多少条"], [{"action": "count"}], ["103 条"]),
        "lookup": (["查询关于茶的记忆"], [{"action": "list", "query": "茶"}], ["我喜欢茶"]),
        "delete_many": (
            ["列出我的记忆", "删除第一条和第二条记忆", "确认删除"],
            [{"action": "list"}, {"action": "delete_many", "selections": [1, 2]}],
            ["第2条", "将删除以下 2 条", "已删除选中的 2 条"],
        ),
        "delete_all": (
            ["清空我的所有记忆", "确认清空"],
            [{"action": "delete_all"}],
            ["全部 103 条", "本次删除 103 条"],
        ),
    }
    utterances, plans, expected = scenarios[operation]

    class CommandSpeech(FakeSpeech):
        def __init__(self, config):
            super().__init__(config)
            self.utterances = iter(enumerate(utterances))

        async def append_audio(self, ws, pcm):
            index, text = next(self.utterances)
            item_id = f"memory-{index}"
            await self.events.put({"type": "input_audio_buffer.speech_started", "item_id": item_id})
            for _ in range(2):
                await self.events.put(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": item_id,
                        "transcript": text,
                    }
                )

    class PlanningModel(FakeListChatModel):
        def with_structured_output(self, schema, **kwargs):
            return self | RunnableLambda(
                lambda message: schema.model_validate_json(message.content)
            )

    records = [
        MemoryRecord(id=uuid4(), memory=text, user_id="alice", agent_id="mem0-chatbot")
        for text in ["我喜欢茶", "我住在上海"]
    ]
    memory = AsyncMock(spec=Mem0Service)
    memory.list_memories.return_value = MemoryList(memories=records)
    memory.search_memory_records.return_value = MemoryList(memories=records[:1])
    memory.get_memory.side_effect = lambda memory_id, _: next(
        record for record in records if str(record.id) == memory_id
    )
    memory.memory_snapshot.return_value = MemorySnapshot(count=103, revision="snapshot")
    memory.apply_memory_mutation.return_value = MemoryMutationResult(
        status="cleared" if operation == "delete_all" else "deleted",
        deleted_count=103 if operation == "delete_all" else 2,
    )
    model = PlanningModel(responses=[json.dumps(plan) for plan in plans])
    monkeypatch.setattr(module, "get_mem0_service", lambda: memory)
    monkeypatch.setattr(module, "get_model", lambda _, **kwargs: model)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", CommandSpeech)
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    agents["mem0-chatbot"] = graph
    auth = headers()
    session = create(client, auth, agent_id="mem0-chatbot")
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        for index, expected_text in enumerate(expected):
            ws.send_bytes(
                encode_audio(
                    b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, index, kind=1
                )
            )
            result = read_until(ws, "response.done")
            spoken = "".join(item["text"] for item in result if item["type"] == "text.delta")
            assert expected_text in spoken
            assert '"action"' not in spoken and '"selections"' not in spoken
            assert any(item["type"] == "audio" for item in result)
            if utterances[index] not in {"确认删除", "确认清空"}:
                memory.apply_memory_mutation.assert_not_awaited()
            event(ws, "playback.finished", response_id=result[-1]["response_id"])
        event(ws, "session.close")
        read_until(ws, "session.closed")
    assert len(repo.records) == len(utterances)
    memory.remember.assert_not_awaited()
    if operation in {"count", "lookup"}:
        memory.apply_memory_mutation.assert_not_awaited()
    else:
        memory.apply_memory_mutation.assert_awaited_once()
        user_id, mutation = memory.apply_memory_mutation.await_args.args
        assert user_id == "alice" and mutation.action == operation
        if operation == "delete_many":
            assert {target.id for target in mutation.targets} == {record.id for record in records}
        else:
            assert mutation.revision == "snapshot"


@pytest.mark.parametrize("agent_id", ["mem0-chatbot", "chatbot"])
@pytest.mark.parametrize("delivery_status", ["sent", "unknown", "scheduled"])
def test_audio_transcript_sends_email_and_speaks_actual_result(
    voice_env, monkeypatch, agent_id, delivery_status
):
    import importlib

    import app.api.voice as voice_routes
    from app.core.skills import EMAIL_SKILL_NAME, EmailSkill
    from app.schemas.email_skill import EmailSendResult
    from app.services.skills import email

    module = importlib.import_module("app.agents.mem0_chat")
    client, repo, agents, headers = voice_env

    class EmailSpeech(FakeSpeech):
        async def append_audio(self, ws, pcm):
            for _ in range(2):
                await self.events.put(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "email-audio-input",
                        "transcript": ("十分钟后" if delivery_status == "scheduled" else "")
                        + "给我发邮件，主题是会议提醒，正文是明天下午三点开会。",
                    }
                )

    class EmailModel(FakeListChatModel):
        def with_structured_output(self, schema, **kwargs):
            return self | RunnableLambda(
                lambda message: schema.model_validate_json(message.content)
            )

    target = datetime.now(UTC) + timedelta(minutes=10)
    service = Mock(
        send=AsyncMock(
            return_value=EmailSendResult(
                status=delivery_status, send_at=target if delivery_status == "scheduled" else None
            )
        )
    )
    monkeypatch.setattr(email, "_service", service)
    monkeypatch.setattr(settings, "EMAIL_SKILL_ENABLED", True)
    monkeypatch.setattr(
        settings, "_email_skill", EmailSkill(EMAIL_SKILL_NAME, "发邮件", "Workflow", "digest")
    )
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", EmailSpeech)
    intent = {"action": "send", "subject": "会议提醒", "body": "明天下午三点开会。"}
    if delivery_status == "scheduled":
        intent.update(timing="scheduled", send_at=target.isoformat())
    model = EmailModel(responses=[json.dumps(intent, ensure_ascii=False)])
    monkeypatch.setattr(module, "get_model", lambda _, **kwargs: model)
    monkeypatch.setattr(module, "get_mem0_service", lambda: None)
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    agents[agent_id] = graph
    auth = headers("7")
    session = create(client, auth, agent_id=agent_id)
    with connect(client, session, auth) as ws:
        ready = configure(ws)
        ws.send_bytes(
            encode_audio(b"\0\0" * 320, ready["connection_id"], str(ZERO_UUID), 0, 0, kind=1)
        )
        events = read_until(ws, "response.done")
        spoken = "".join(item["text"] for item in events if item["type"] == "text.delta")
        assert any(item["type"] == "audio" for item in events)
        expected = {"sent": "提交发送", "unknown": "不确定", "scheduled": "已预约"}
        assert expected[delivery_status] in spoken
        assert '"action"' not in spoken and "Workflow" not in spoken
        event(ws, "session.close")
        read_until(ws, "session.closed")
    service.send.assert_awaited_once()
    user_id, request_id, content = service.send.await_args.args
    assert user_id == "7" and request_id.startswith("voice-input-")
    assert content.subject == "会议提醒" and content.body == "明天下午三点开会。"
    if delivery_status == "scheduled":
        assert service.send.await_args.kwargs["send_at"] == target
    assert len(repo.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_activation", [True, False])
async def test_audio_pacing_accounts_for_declared_wake_preroll(wake_activation):
    from app.api.voice_connection import VoiceConnection
    from app.schemas.voice import VoiceSession

    session = VoiceSession(
        user_id="synthetic-wake",
        voice="Cherry",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        activation="wake_word" if wake_activation else "tap",
        wake_word="小美小美" if wake_activation else None,
        pre_roll_samples=64000 if wake_activation else 0,
    )
    websocket = Mock()
    connection = VoiceConnection(websocket, session, Mock(), Mock(), Mock(), settings)
    frames = [
        {
            "type": "websocket.receive",
            "bytes": encode_audio(
                b"\0\0" * 3200, connection.connection_id, str(ZERO_UUID), 0, sequence, kind=1
            ),
        }
        for sequence in range(25)
    ]
    websocket.receive = AsyncMock(side_effect=[*frames, {"type": "websocket.disconnect"}])
    if wake_activation:
        await connection.receiver()
        assert connection.audio_in.qsize() == 25
        assert connection.input_samples == 80000
    else:
        with pytest.raises(ValueError, match="paced in realtime"):
            await connection.receiver()


def test_wake_preroll_can_cover_a_slow_double_call(voice_env, monkeypatch):
    client, _, _, headers = voice_env
    monkeypatch.setattr(settings, "VOICE_WAKE_PRE_ROLL_MS", 4000)
    auth = headers()
    capabilities = client.get("/voice/capabilities", headers=auth).json()["data"]
    assert capabilities["wake_word"]["pre_roll_ms"] == 4000
    session = create(
        client, auth, activation="wake_word", wake_word="小美小美", pre_roll_samples=64000
    )
    assert session["pre_roll_samples"] == 64000
    rejected = client.post(
        "/voice/sessions",
        headers=auth,
        json={"activation": "wake_word", "wake_word": "小美小美", "pre_roll_samples": 96000},
    )
    assert rejected.status_code == 422


@pytest.mark.parametrize("agent_id", ["mem0-chatbot", "chatbot"])
def test_audio_video_requests_remain_ordinary_chat(voice_env, monkeypatch, agent_id):
    import importlib

    import app.api.voice as voice_routes
    from app.services.video import runtime

    module = importlib.import_module("app.agents.mem0_chat")
    client, repo, agents, headers = voice_env

    class VideoSpeech(FakeSpeech):
        def __init__(self, config):
            super().__init__(config)
            self.input_count = 0

        async def append_audio(self, ws, pcm):
            self.input_count += 1
            transcript = (
                "生成一个五秒海边日落的视频" if self.input_count == 1 else "刚才的视频好了吗"
            )
            for _ in range(2):
                await self.events.put(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": f"video-audio-{self.input_count}",
                        "transcript": transcript,
                    }
                )

    service = Mock(create=AsyncMock(), query=AsyncMock(), list_tasks=AsyncMock())
    monkeypatch.setattr(runtime, "_service", service)
    monkeypatch.setattr(settings, "VIDEO_ENABLED", True)
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", VideoSpeech)
    model = FakeListChatModel(responses=["普通聊天回答。", "继续普通聊天。"])
    monkeypatch.setattr(module, "get_model", lambda *_, **kwargs: model)
    monkeypatch.setattr(module, "get_mem0_service", lambda: None)
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    agents[agent_id] = graph
    auth = headers("7")
    session = create(client, auth, agent_id=agent_id)
    with connect(client, session, auth) as ws:
        connection = configure(ws)["connection_id"]
        for sequence, expected in enumerate(["普通聊天回答。", "继续普通聊天。"]):
            ws.send_bytes(
                encode_audio(b"\0\0" * 320, connection, str(ZERO_UUID), 0, sequence, kind=1)
            )
            seen = read_until(ws, "response.done")
            spoken = "".join(item["text"] for item in seen if item["type"] == "text.delta")
            assert spoken == expected
            assert any(item["type"] == "audio" for item in seen)
            assert sum(item["type"] == "transcript.final" for item in seen) == 1
            assert not any(
                item["type"] == "agent.custom" and item["data"]["type"].startswith("video.")
                for item in seen
            )
            event(ws, "playback.finished", response_id=seen[-1]["response_id"])
        event(ws, "session.close")
        read_until(ws, "session.closed")
    service.create.assert_not_awaited()
    service.query.assert_not_awaited()
    service.list_tasks.assert_not_awaited()
    assert len(repo.records) == 2


@pytest.mark.parametrize("agent_id", ["mem0-chatbot", "chatbot"])
def test_audio_triggers_managed_video_skill_for_create_list_detail_and_edit(
    voice_env, monkeypatch, agent_id
):
    import importlib

    import app.api.voice as voice_routes
    from app.core.skills import SkillDefinition
    from app.schemas.video import VideoTask, VideoTaskPage
    from app.services.video import runtime

    client, repo, agents, headers = voice_env
    module = importlib.import_module("app.agents.mem0_chat")
    transcripts = ["生成五秒海边视频", "找海边有海鸥的视频", "查询第二条", "把刚才视频改成水墨风格"]

    class VideoSpeech(FakeSpeech):
        def __init__(self, config):
            super().__init__(config)
            self.input_count = 0

        async def append_audio(self, ws, pcm):
            transcript = transcripts[self.input_count]
            self.input_count += 1
            for _ in range(2):
                await self.events.put(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": f"skill-audio-{self.input_count}",
                        "transcript": transcript,
                    }
                )

    class VideoModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    pending = VideoTask(
        id=123,
        user_id=7,
        task_id="task-123",
        status="PENDING",
        storage_status="NOT_READY",
        aliyun_video_url=None,
        qiniu_video_url=None,
        qiniu_object_key=None,
        error_code=None,
        create_time=datetime.now(UTC),
        update_time=datetime.now(UTC),
    )
    ready = pending.model_copy(
        update={
            "status": "SUCCEEDED",
            "storage_status": "STORED",
            "qiniu_video_url": "https://cdn.example/video.mp4",
            "title": "海边海鸥",
            "cover_url": "https://cdn.example/cover.jpg",
        }
    )
    ready = VideoTask.model_validate(ready.model_dump())
    service = Mock(
        create=AsyncMock(return_value=pending),
        query=AsyncMock(return_value=ready),
        edit=AsyncMock(return_value=pending),
    )
    service.list_tasks = AsyncMock(
        return_value=VideoTaskPage(
            items=[ready.model_copy(update={"id": 124}), ready],
            total=2,
            page=1,
            page_size=5,
            total_pages=1,
        )
    )
    monkeypatch.setattr(runtime, "_service", service)
    monkeypatch.setattr(settings, "VIDEO_ENABLED", True)
    monkeypatch.setattr(settings, "VIDEO_SKILL_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "_video_skill",
        SkillDefinition("manage-user-videos", "视频管理", "受控视频技能", "a" * 64),
    )
    monkeypatch.setattr(voice_routes, "AlibabaRealtime", VideoSpeech)
    operations = [
        {"action": "create", "authorized": True, "generation": {"prompt": "海边", "duration": 5}},
        {"action": "list", "query": {"q": "海边 海鸥"}},
        {"action": "detail", "selection": 2},
        {"action": "edit", "authorized": True, "editing": {"prompt": "水墨风格"}},
    ]
    model = VideoModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"name": name, "args": args, "id": f"{index}-{name}"}]
            )
            for index, operation in enumerate(operations)
            for name, args in [("load_video_skill", {}), ("video_operation", operation)]
        ]
    )
    monkeypatch.setattr(module, "get_model", lambda *_, **kwargs: model)
    monkeypatch.setattr(module, "get_mem0_service", lambda: None)
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    agents[agent_id] = graph
    auth = headers("7")
    session = create(client, auth, agent_id=agent_id)
    with connect(client, session, auth) as ws:
        connection = configure(ws)["connection_id"]
        for sequence in range(4):
            ws.send_bytes(
                encode_audio(b"\0\0" * 320, connection, str(ZERO_UUID), 0, sequence, kind=1)
            )
            seen = read_until(ws, "response.done")
            assert sum(item["type"] == "transcript.final" for item in seen) == 1
            assert any(item["type"] == "audio" for item in seen)
            spoken = "".join(item["text"] for item in seen if item["type"] == "text.delta")
            assert '"action"' not in spoken and "https://" not in spoken
            data = next(
                item["data"]
                for item in seen
                if item["type"] == "agent.custom" and item["data"]["type"].startswith("video.")
            )
            assert data["type"] == ("video.list" if sequence == 1 else "video.task")
            if sequence == 1:
                assert data["total"] == 2 and "2 个" in spoken
                assert data["query"]["q"] == "海边 海鸥"
                assert data["page_size"] == 5
                assert data["items"][0]["title"] == "海边海鸥"
                assert data["items"][0]["cover_url"] == "https://cdn.example/cover.jpg"
                assert data["items"][0]["qiniu_video_url"] == "https://cdn.example/video.mp4"
                assert "海边海鸥" in spoken
            event(ws, "playback.finished", response_id=seen[-1]["response_id"])
        event(ws, "session.close")
        read_until(ws, "session.closed")
    service.create.assert_awaited_once()
    assert service.create.await_args.args[2].watermark is False
    service.list_tasks.assert_awaited_once()
    assert service.list_tasks.await_args.args[1].q == "海边 海鸥"
    assert service.list_tasks.await_args.args[1].page_size == 5
    service.query.assert_awaited_once_with(123, 7)
    service.edit.assert_awaited_once()
    assert service.edit.await_args.args[2] == 123
    assert len(repo.records) == 4


def test_execution_snapshots_recover_with_thread_authorization(voice_env, monkeypatch):
    import importlib

    from langgraph.store.memory import InMemoryStore

    module = importlib.import_module("app.agents.mem0_chat")
    client, _, agents, headers = voice_env
    monkeypatch.setattr(
        module, "get_model", lambda *args, **kwargs: FakeListChatModel(responses=["你好。"])
    )
    monkeypatch.setattr(module, "get_mem0_service", lambda: None)
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    graph.store = InMemoryStore()
    agents["chatbot"] = graph
    agents["mem0-chatbot"] = graph
    auth = headers("7")
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="你好")
        seen = read_until(ws, "response.done")
        progress_events = [item["data"] for item in seen if item["type"] == "agent.custom"]
        assert progress_events[-1]["status"] == "succeeded"
        assert [item["sequence"] for item in progress_events] == sorted(
            {item["sequence"] for item in progress_events}
        )
        event(ws, "session.close")
        read_until(ws, "session.closed")
    query = {"thread_id": session["thread_id"]}
    recovered = client.get("/chatbot/executions", headers=auth, params=query)
    assert recovered.status_code == 200
    snapshots = recovered.json()["data"]["executions"]
    assert snapshots[-1]["text"] == "你好。"
    assert snapshots[-1]["response_id"] == progress_events[-1]["response_id"]
    assert client.get("/chatbot/executions", headers=headers("8"), params=query).status_code == 403
    assert client.get("/mem0-chatbot/executions", headers=auth, params=query).status_code == 403
    assert client.get("/chatbot/executions", params=query).status_code == 401
    history = client.post("/chatbot/history", headers=auth, json=query).json()["data"]["messages"]
    assert history[-1]["response_metadata"]["execution"]["run_id"] == snapshots[-1]["run_id"]


def test_memory_confirmation_buttons_are_bound_to_current_operation(voice_env, monkeypatch):
    import importlib

    from langgraph.store.memory import InMemoryStore

    from app.agents import memory_commands
    from app.schemas.memory import MemoryList, MemoryRecord
    from app.schemas.memory_commands import MemoryIntent, MemoryMutationResult

    module = importlib.import_module("app.agents.mem0_chat")
    client, _, agents, headers = voice_env
    record = MemoryRecord(id=uuid4(), memory="喜欢茶", user_id="7", agent_id="mem0-chatbot")
    memory = AsyncMock()
    memory.search_memory_records.return_value = MemoryList(memories=[record])
    memory.apply_memory_mutation.return_value = MemoryMutationResult(
        status="deleted", deleted_count=1
    )
    monkeypatch.setattr(
        memory_commands,
        "parse_memory_intent",
        AsyncMock(return_value=MemoryIntent(action="delete", query="茶")),
    )
    monkeypatch.setattr(
        memory_commands, "match_memories", AsyncMock(return_value=[memory_commands._target(record)])
    )
    monkeypatch.setattr(
        module, "get_model", lambda *args, **kwargs: FakeListChatModel(responses=["你好"])
    )
    monkeypatch.setattr(module, "get_mem0_service", lambda: memory)
    graph = module.mem0_chat.copy()
    graph.checkpointer = MemorySaver()
    graph.store = InMemoryStore()
    agents["chatbot"] = graph
    agents["mem0-chatbot"] = graph
    auth = headers("7")
    session = create(client, auth)
    with connect(client, session, auth) as ws:
        configure(ws)
        event(ws, "input.text", text="删除我喜欢茶的记忆")
        seen = read_until(ws, "response.done")
        final = [item["data"] for item in seen if item["type"] == "agent.custom"][-1]
        assert final["status"] == "waiting_confirmation"
        confirmation = final["confirmation"]["confirmation_id"]
        event(ws, "memory.confirm", confirmation_id="0" * 64, approved=True)
        read_until(ws, "response.done")
        memory.apply_memory_mutation.assert_not_awaited()
        event(ws, "memory.confirm", confirmation_id=confirmation, approved=True)
        read_until(ws, "response.done")
        memory.apply_memory_mutation.assert_awaited_once()
        assert memory.apply_memory_mutation.await_args.args[0] == "7"
        event(ws, "memory.confirm", confirmation_id=confirmation, approved=True)
        read_until(ws, "response.done")
        memory.apply_memory_mutation.assert_awaited_once()
        event(ws, "session.close")
        read_until(ws, "session.closed")
    restored = client.get(
        "/chatbot/executions", headers=auth, params={"thread_id": session["thread_id"]}
    ).json()["data"]["executions"]
    assert not any(item["confirmation"] for item in restored)
    assert restored[0]["status"] == "cancelled"
