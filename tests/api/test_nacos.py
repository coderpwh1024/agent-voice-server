import os
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from v2.nacos.ai.model.prompt.prompt import Prompt

import app.core.nacos as nacos
from app.core.settings import Settings
from app.core.speech import voice_options


def test_file_upload_settings_map_from_nacos() -> None:
    config = nacos.Settings(_env_file=None, USE_FAKE_MODEL=True)
    nacos.apply_remote_settings(
        config, "storage:\n  files:\n    enabled: true\n    max_bytes: 1048576\n"
    )
    assert config.FILE_UPLOAD_ENABLED is True
    assert config.FILE_UPLOAD_MAX_BYTES == 1048576


def test_device_memory_settings_map_independently_of_mem0() -> None:
    config = nacos.Settings(_env_file=None, USE_FAKE_MODEL=True)
    nacos.apply_remote_settings(
        config,
        """memory:
  local:
    enabled: false
    extraction_timeout_seconds: 12
    context_timeout_seconds: 2
""",
    )
    assert config.LOCAL_MEMORY_ENABLED is False
    assert config.LOCAL_MEMORY_EXTRACTION_TIMEOUT_SECONDS == 12
    assert config.LOCAL_MEMORY_CONTEXT_TIMEOUT_SECONDS == 2
    assert config.MEM0_ENABLED is False


@pytest.fixture(autouse=True)
def ai_client(monkeypatch, mem0_prompts):
    client = AsyncMock()
    templates = mem0_prompts.model_dump()
    keys = nacos.Settings(_env_file=None, USE_FAKE_MODEL=True).MEM0_PROMPT_KEYS.model_dump()
    by_key = {key: templates[name] for name, key in keys.items()}
    client.get_prompt.side_effect = lambda param: Prompt(
        promptKey=param.prompt_key, version="1.0.0", template=by_key[param.prompt_key]
    )
    monkeypatch.setattr(nacos.NacosAIService, "create_ai_service", AsyncMock(return_value=client))
    return client


def make_settings(**overrides) -> Settings:
    return Settings(USE_FAKE_MODEL=True, _env_file=None, **overrides)


def test_mem0_research_settings_map_from_nacos():
    config = make_settings()
    nacos.apply_remote_settings(
        config,
        "memory:\n  mem0:\n    research:\n      enabled: false\n"
        "      timeout_seconds: 12\n      final_timeout_seconds: 10\n"
        "      search_timeout_seconds: 3\n",
    )
    assert not config.MEM0_RESEARCH_ENABLED
    assert config.MEM0_RESEARCH_TIMEOUT_SECONDS == 12
    assert config.MEM0_RESEARCH_FINAL_TIMEOUT_SECONDS == 10
    assert config.RESEARCH_SEARCH_TIMEOUT_SECONDS == 3


@pytest.mark.asyncio
async def test_email_skill_downloads_published_version_and_rejects_bad_archive(ai_client):
    from io import BytesIO
    from pathlib import Path
    from zipfile import ZipFile

    config = make_settings(
        NACOS_ENABLED=True, EMAIL_SKILL_ENABLED=True, EMAIL_SKILL_VERSION="1.0.0"
    )
    integration = nacos.NacosIntegration(config)
    integration.ai_client = ai_client
    buffer = BytesIO()
    with ZipFile(buffer, "w") as package:
        package.writestr("SKILL.md", Path("skills/send-current-user-email/SKILL.md").read_text())
    ai_client.download_skill_zip.return_value = buffer.getvalue()
    await integration.load_remote_skills()
    assert config.email_skill.name == "send-current-user-email"
    param = ai_client.download_skill_zip.await_args.args[0]
    assert param.version == "1.0.0" and param.label is None
    ai_client.download_skill_zip.return_value = b"invalid"
    with pytest.raises(ValueError):
        await integration.load_remote_skills()
    assert config.email_skill is None


def test_email_skill_settings_map_from_nacos():
    config = make_settings()
    nacos.apply_remote_settings(
        config,
        "skills:\n  email:\n    enabled: true\n    version: '1.0.0'\n    label: stable\n    timeout_seconds: 15\n    timezone: Asia/Shanghai\n",
    )
    assert config.EMAIL_SKILL_ENABLED
    assert config.EMAIL_SKILL_VERSION == "1.0.0"
    assert config.EMAIL_SKILL_LABEL == "stable"
    assert config.EMAIL_SKILL_TIMEOUT_SECONDS == 15
    assert config.EMAIL_SKILL_TIMEZONE == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_video_skill_downloads_pinned_version_and_fails_closed(ai_client):
    from io import BytesIO
    from pathlib import Path
    from zipfile import ZipFile

    config = make_settings(
        NACOS_ENABLED=True,
        VIDEO_ENABLED=True,
        VIDEO_SKILL_ENABLED=True,
        VIDEO_SKILL_VERSION="1.0.0",
    )
    integration = nacos.NacosIntegration(config)
    integration.ai_client = ai_client
    buffer = BytesIO()
    with ZipFile(buffer, "w") as package:
        package.writestr("SKILL.md", Path("skills/manage-user-videos/SKILL.md").read_text())
    ai_client.download_skill_zip.return_value = buffer.getvalue()
    await integration.load_remote_skills()
    assert config.video_skill.name == "manage-user-videos"
    param = ai_client.download_skill_zip.await_args.args[0]
    assert param.version == "1.0.0" and param.label is None
    ai_client.download_skill_zip.return_value = b"invalid"
    with pytest.raises(ValueError):
        await integration.load_remote_skills()
    assert config.video_skill is None


def test_video_skill_settings_map_from_nacos():
    config = make_settings()
    nacos.apply_remote_settings(
        config,
        "skills:\n  video:\n    enabled: true\n    version: '1.0.0'\n    label: stable\n    timeout_seconds: 15\n",
    )
    assert config.VIDEO_SKILL_ENABLED and config.VIDEO_SKILL_VERSION == "1.0.0"
    assert config.VIDEO_SKILL_LABEL == "stable" and config.VIDEO_SKILL_TIMEOUT_SECONDS == 15


@pytest.mark.asyncio
async def test_video_skill_requires_nacos_and_video_service():
    config = make_settings(VIDEO_SKILL_ENABLED=True, VIDEO_ENABLED=True, NACOS_ENABLED=False)
    with pytest.raises(ValueError, match="Nacos"):
        await nacos.NacosIntegration(config).load_remote_config()
    config.NACOS_ENABLED = True
    config.VIDEO_ENABLED = False
    with pytest.raises(ValueError, match="视频服务"):
        await nacos.NacosIntegration(config).load_remote_skills()


@pytest.mark.asyncio
async def test_prompt_sdk_loads_all_published_templates(ai_client, mem0_prompts, tmp_path) -> None:
    config = make_settings(
        NACOS_ENABLED=True,
        NACOS_CACHE_DIR=str(tmp_path / "cache"),
        NACOS_LOG_DIR=str(tmp_path / "logs"),
    )
    integration = nacos.NacosIntegration(config)
    try:
        await integration.load_remote_prompts()
        assert config.mem0_prompts == mem0_prompts
        requests = [call.args[0] for call in ai_client.get_prompt.await_args_list]
        assert [request.prompt_key for request in requests] == list(
            config.MEM0_PROMPT_KEYS.model_dump().values()
        )
        assert all(request.label == "latest" for request in requests)
    finally:
        await integration.shutdown()
    ai_client.shutdown.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        Prompt(),
        Prompt(promptKey="wrong", version="1.0.0", template="rules"),
        Prompt(promptKey="mem0-fact-extraction-system", template="rules"),
        Prompt(promptKey="mem0-fact-extraction-system", version="1.0.0", template=""),
        Prompt(promptKey="mem0-fact-extraction-system", version="1.0.0", template=" \n "),
    ],
)
async def test_invalid_prompt_never_installs_partial_templates(
    ai_client, mem0_prompts, tmp_path, invalid
) -> None:
    config = make_settings(
        NACOS_ENABLED=True,
        NACOS_CACHE_DIR=str(tmp_path / "cache"),
        NACOS_LOG_DIR=str(tmp_path / "logs"),
    )
    config.mem0_prompts = mem0_prompts
    original = ai_client.get_prompt.side_effect
    ai_client.get_prompt.side_effect = lambda param: (
        invalid if param.prompt_key == "mem0-fact-extraction-system" else original(param)
    )
    integration = nacos.NacosIntegration(config)
    try:
        with pytest.raises((RuntimeError, ValueError)):
            await integration.load_remote_prompts()
        assert config.mem0_prompts is mem0_prompts
    finally:
        await integration.shutdown()


@pytest.mark.asyncio
async def test_prompt_provider_failure_does_not_fall_back(ai_client, tmp_path) -> None:
    config = make_settings(
        NACOS_ENABLED=True,
        NACOS_CACHE_DIR=str(tmp_path / "cache"),
        NACOS_LOG_DIR=str(tmp_path / "logs"),
    )
    ai_client.get_prompt.side_effect = ConnectionError("unavailable")
    integration = nacos.NacosIntegration(config)
    try:
        with pytest.raises(ConnectionError):
            await integration.load_remote_prompts()
        with pytest.raises(RuntimeError, match="Nacos Prompt"):
            _ = config.mem0_prompts
    finally:
        await integration.shutdown()


@pytest.mark.asyncio
async def test_disabled_nacos_does_not_load_prompts(ai_client) -> None:
    config = make_settings(NACOS_ENABLED=False)
    integration = nacos.NacosIntegration(config)
    await integration.load_remote_prompts()
    ai_client.get_prompt.assert_not_awaited()
    with pytest.raises(RuntimeError, match="Nacos Prompt"):
        _ = config.mem0_prompts


def test_prompt_references_are_configured_without_yaml_templates() -> None:
    config = make_settings()
    nacos.apply_remote_settings(
        config,
        "memory:\n  mem0:\n    prompt_label: stable\n"
        "    prompt_keys:\n      chat_system: custom-mem0-chat\n",
    )
    assert config.MEM0_PROMPT_LABEL == "stable"
    assert config.MEM0_PROMPT_KEYS.chat_system == "custom-mem0-chat"
    assert config.MEM0_PROMPT_KEYS.extraction_system == "mem0-fact-extraction-system"


def test_prompt_templates_cannot_be_supplied_as_main_yaml() -> None:
    with pytest.raises(ValueError, match="Unknown Nacos settings: prompt"):
        nacos.apply_remote_settings(make_settings(), "prompt:\n  mem0:\n    chat_system: rules\n")


def test_apply_remote_settings_validates_and_updates_shared_settings() -> None:
    config = make_settings(RAG_TOP_K=5)

    updated = nacos.apply_remote_settings(
        config,
        "RAG_TOP_K: 9\nRAG_COLLECTION_NAME: nacos-collection\n",
    )

    assert updated == ["RAG_COLLECTION_NAME", "RAG_TOP_K"]
    assert config.RAG_TOP_K == 9
    assert config.RAG_COLLECTION_NAME == "nacos-collection"


def test_nacos_bootstrap_loads_model_provider_from_remote_config() -> None:
    with patch.dict(os.environ, {}, clear=True):
        config = Settings(
            _env_file=None,
            NACOS_ENABLED=True,
            NACOS_CONFIG_DATA_ID="agent-service-toolkit.yaml",
        )

    nacos.apply_remote_settings(config, "DASHSCOPE_API_KEY: remote-key\n")

    assert config.DASHSCOPE_API_KEY == SecretStr("remote-key")
    assert config.AVAILABLE_MODELS


def test_wake_configuration_supports_greeting_and_confirmation_timeout() -> None:
    config = make_settings()
    nacos.apply_remote_settings(
        config,
        """voice:
  wake:
    word: 小美，小美
    greeting: 我在，请讲。
    pre_roll_ms: 2200
    confirm_seconds: 8
""",
    )
    assert config.VOICE_WAKE_WORD == "小美小美"
    assert config.VOICE_WAKE_GREETING == "我在，请讲。"
    assert config.VOICE_WAKE_PRE_ROLL_MS == 2200
    assert config.VOICE_WAKE_CONFIRM_SECONDS == 8


def test_nacos_remote_config_must_resolve_model_provider() -> None:
    with patch.dict(os.environ, {}, clear=True):
        config = Settings(
            _env_file=None,
            NACOS_ENABLED=True,
            NACOS_CONFIG_DATA_ID="agent-service-toolkit.yaml",
        )

    with pytest.raises(ValueError, match="At least one LLM API key must be provided"):
        nacos.apply_remote_settings(config, "RAG_TOP_K: 7\n")


def test_nacos_voice_catalog_and_protocol_overrides_are_applied() -> None:
    config = make_settings()
    updated = nacos.apply_remote_settings(
        config,
        "voice:\n  enabled: true\n  realtime:\n    stt_model: qwen-audio-3.1-asr-flash-message\n    tts_model: custom-tts\n    tts_protocol: inference\n    stt_url: wss://speech.example.com/asr\n    tts_url: wss://speech.example.com/tts\n    voices_model: old-model\n    voice_catalog:\n      custom-tts:\n        - id: custom-voice\n          name: 自定义音色\n          languages: [zh]\n",
    )
    assert config.VOICE_REALTIME_TTS_PROTOCOL == "inference"
    assert config.VOICE_REALTIME_STT_URL == "wss://speech.example.com/asr"
    assert config.VOICE_REALTIME_TTS_URL == "wss://speech.example.com/tts"
    assert "VOICE_REALTIME_VOICE_CATALOG" in updated
    assert voice_options(config)[0].id == "custom-voice"
    assert voice_options(config)[0].languages == ["zh"]


def test_nested_nacos_config_maps_sections_to_settings() -> None:
    config = make_settings()

    updated = nacos.apply_remote_settings(
        config,
        """models:
  dashscope:
    api_key: remote-key
authentication:
  email:
    enabled: true
storage:
  redis:
    url: redis://remote:6379/0
voice:
  enabled: true
  llm:
    enable_thinking: true
  realtime:
    voices:
      - Cherry
  session:
    max_sessions: 12
  vad:
    silence_ms: 750
rag:
  top_k: 9
""",
    )

    assert updated == [
        "DASHSCOPE_API_KEY",
        "EMAIL_AUTH_ENABLED",
        "RAG_TOP_K",
        "REDIS_URL",
        "VOICE_ENABLED",
        "VOICE_LLM_ENABLE_THINKING",
        "VOICE_MAX_SESSIONS",
        "VOICE_REALTIME_VOICES",
        "VOICE_VAD_SILENCE_MS",
    ]
    assert config.DASHSCOPE_API_KEY == SecretStr("remote-key")
    assert config.EMAIL_AUTH_ENABLED is True
    assert config.REDIS_URL == SecretStr("redis://remote:6379/0")
    assert config.VOICE_ENABLED is True
    assert config.VOICE_LLM_ENABLE_THINKING is True
    assert [voice.id for voice in config.VOICE_REALTIME_VOICES] == ["Cherry"]
    assert config.VOICE_REALTIME_VOICES[0].name == "Cherry"
    assert config.VOICE_MAX_SESSIONS == 12
    assert config.VOICE_VAD_SILENCE_MS == 750
    assert config.RAG_TOP_K == 9


def test_complete_nested_nacos_yaml_is_accepted() -> None:
    config = make_settings()

    updated = nacos.apply_remote_settings(
        config,
        """models:
  dashscope:
    api_key: remote-key
    base_url: https://dashscope.example.com/v1
    embedding_model: embedding-model
authentication:
  admin_secret: remote-admin-secret
  service_account_id: 0
  app_token:
    secret: remote-app-token-secret-at-least-32-characters
    ttl_seconds: 1296000
  email:
    enabled: true
    smtp:
      host: smtp.example.com
      port: 465
      username: mailer@example.com
      password: remote-password
      from_email: mailer@example.com
      use_tls: false
      use_ssl: true
storage:
  database_type: postgres
  redis:
    url: redis://remote:6379/0
  postgres:
    user: remote-user
    password: remote-password
    host: remote-postgres
    port: 5432
    database: agent_service
    application_name: agent-service-toolkit
    pool:
      min_connections: 1
      max_connections: 1
  qiniu:
    ak: qiniu-access-key
    sk: qiniu-secret-key
    bucket_name: agent-service-toolkit-avatars
    public_base_url: https://cdn.example.com
    upload_token_ttl_seconds: 3600
    avatar_max_bytes: 5242880
rag:
  collection_name: remote-collection
  top_k: 5
voice:
  enabled: true
  realtime:
    url: wss://dashscope.example.com/realtime
    proxy: null
    stt_model: realtime-stt
    tts_model: realtime-tts
    stt_protocol: realtime
    tts_protocol: realtime
    voices_model: realtime-tts
    voices:
      - id: Cherry
        name: 芊悦
        description: 阳光积极、亲切自然
      - id: Serena
        name: 苏瑶
        description: 温柔自然
    upstream_timeout: 30
  session:
    max_sessions: 8
    duration_seconds: 1800
    idle_seconds: 120
    queue_size: 64
  vad:
    silence_ms: 500
    threshold: 0.2
""",
        required_fields=nacos._REMOTE_STORAGE_FIELDS,
    )

    assert "VOICE_ENABLED" in updated
    assert config.VOICE_ENABLED is True
    assert [voice.id for voice in config.VOICE_REALTIME_VOICES] == ["Cherry", "Serena"]
    assert config.VOICE_REALTIME_VOICES[0].name == "芊悦"
    assert config.VOICE_REALTIME_VOICES[1].description == "温柔自然"
    assert config.POSTGRES_DB == "agent_service"
    assert config.QINIU_ACCESS_KEY == SecretStr("qiniu-access-key")
    assert config.QINIU_SECRET_KEY == SecretStr("qiniu-secret-key")
    assert config.QINIU_BUCKET_NAME == "agent-service-toolkit-avatars"
    assert config.APP_TOKEN_TTL_SECONDS == 1_296_000


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("voice:\n  unknown: true\n", "Unknown Nacos voice settings"),
        (
            "voice:\n  enabled: true\nVOICE_ENABLED: false\n",
            "conflicts with VOICE_ENABLED",
        ),
    ],
)
def test_nested_nacos_config_rejects_invalid_sections(content: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        nacos.apply_remote_settings(make_settings(), content)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("- item", "YAML mapping"),
        ("UNKNOWN_SETTING: true", "Unknown Nacos settings"),
        ("APP_ENV: test", "bootstrap settings"),
        ("PORT: 9000", "bootstrap settings"),
        ("NACOS_SERVICE_NAME: other", "bootstrap settings"),
        ("1: value", "keys must be strings"),
    ],
)
def test_apply_remote_settings_rejects_invalid_payloads(content: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        nacos.apply_remote_settings(make_settings(), content)


@pytest.mark.asyncio
async def test_load_remote_config(monkeypatch, tmp_path) -> None:
    config = make_settings(
        NACOS_ENABLED=True,
        NACOS_CONFIG_DATA_ID="agent-service-toolkit.yaml",
        NACOS_STORAGE_CONFIG_REQUIRED=False,
        NACOS_USERNAME="nacos",
        NACOS_PASSWORD=SecretStr("secret"),
        NACOS_CACHE_DIR=str(tmp_path / "cache"),
        NACOS_LOG_DIR=str(tmp_path / "logs"),
    )
    config_client = AsyncMock()
    config_client.get_config.return_value = "RAG_TOP_K: 7\n"
    create_config_service = AsyncMock(return_value=config_client)
    monkeypatch.setattr(
        nacos.NacosConfigService,
        "create_config_service",
        create_config_service,
    )

    integration = nacos.NacosIntegration(config)
    await integration.load_remote_config()

    assert config.RAG_TOP_K == 7
    request = config_client.get_config.await_args.args[0]
    assert request.data_id == "agent-service-toolkit.yaml"
    assert request.group == "DEFAULT_GROUP"
    client_config = create_config_service.await_args.args[0]
    assert client_config.server_list == ["127.0.0.1:8848"]
    assert client_config.username == "nacos"
    assert client_config.password == "secret"
    await integration.shutdown()
    config_client.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_discover_and_deregister_service(monkeypatch, tmp_path) -> None:
    config = make_settings(
        NACOS_ENABLED=True,
        NACOS_STORAGE_CONFIG_REQUIRED=False,
        NACOS_SERVICE_IP="10.0.0.7",
        NACOS_SERVICE_PORT=9000,
        NACOS_METADATA={"protocol": "http"},
        NACOS_CACHE_DIR=str(tmp_path / "cache"),
        NACOS_LOG_DIR=str(tmp_path / "logs"),
    )
    naming_client = AsyncMock()
    naming_client.server_health.return_value = True
    naming_client.register_instance.return_value = True
    naming_client.deregister_instance.return_value = True
    naming_client.list_instances.return_value = []
    create_naming_service = AsyncMock(return_value=naming_client)
    monkeypatch.setattr(
        nacos.NacosNamingService,
        "create_naming_service",
        create_naming_service,
    )

    integration = nacos.NacosIntegration(config)
    await integration.register_service()
    assert await integration.list_instances("dependency") == []
    await integration.shutdown()

    registration = naming_client.register_instance.await_args.args[0]
    assert registration.service_name == "agent-service-toolkit"
    assert registration.ip == "10.0.0.7"
    assert registration.port == 9000
    assert registration.metadata == {"protocol": "http"}
    deregistration = naming_client.deregister_instance.await_args.args[0]
    assert deregistration.service_name == registration.service_name
    assert deregistration.ip == registration.ip
    assert deregistration.port == registration.port
    naming_client.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_fails_when_nacos_is_unavailable(monkeypatch, tmp_path) -> None:
    config = make_settings(
        NACOS_ENABLED=True,
        NACOS_STORAGE_CONFIG_REQUIRED=False,
        NACOS_CACHE_DIR=str(tmp_path / "cache"),
        NACOS_LOG_DIR=str(tmp_path / "logs"),
    )
    naming_client = AsyncMock()
    naming_client.server_health.return_value = False
    create_naming_service = AsyncMock(return_value=naming_client)
    monkeypatch.setattr(
        nacos.NacosNamingService,
        "create_naming_service",
        create_naming_service,
    )

    integration = nacos.NacosIntegration(config)
    with pytest.raises(RuntimeError, match="naming service is unavailable"):
        await integration.register_service()
    await integration.shutdown()
    naming_client.shutdown.assert_awaited_once()


def test_remote_storage_config_must_be_complete() -> None:
    config = make_settings()

    with pytest.raises(ValueError, match="POSTGRES_DB"):
        nacos.apply_remote_settings(
            config,
            "REDIS_URL: redis://remote:6379/0\n",
            required_fields=nacos._REMOTE_STORAGE_FIELDS,
        )


def test_apply_remote_settings_rejects_malformed_yaml() -> None:
    with pytest.raises(ValueError, match="valid YAML"):
        nacos.apply_remote_settings(make_settings(), "REDIS_URL: [")


def test_remote_email_settings_override_local_values() -> None:
    config = make_settings(
        APP_TOKEN_SECRET="local-token-secret-that-is-long-enough",
        SMTP_HOST="local-smtp",
        SMTP_PORT=587,
        SMTP_USERNAME="local@example.com",
        SMTP_PASSWORD="local-password",
        SMTP_FROM_EMAIL="local@example.com",
        SMTP_USE_TLS=True,
        SMTP_USE_SSL=False,
    )

    nacos.apply_remote_settings(
        config,
        """APP_TOKEN_SECRET: remote-token-secret-that-is-long-enough
SMTP_HOST: smtp.example.com
SMTP_PORT: 465
SMTP_USERNAME: mailer@example.com
SMTP_PASSWORD: remote-password
SMTP_FROM_EMAIL: mailer@example.com
SMTP_USE_TLS: false
SMTP_USE_SSL: true
""",
    )

    assert config.APP_TOKEN_SECRET == SecretStr("remote-token-secret-that-is-long-enough")
    assert config.SMTP_HOST == "smtp.example.com"
    assert config.SMTP_PORT == 465
    assert config.SMTP_USERNAME == "mailer@example.com"
    assert config.SMTP_PASSWORD == SecretStr("remote-password")
    assert str(config.SMTP_FROM_EMAIL) == "mailer@example.com"
    assert config.SMTP_USE_TLS is False
    assert config.SMTP_USE_SSL is True


def test_spring_style_mail_settings_are_mapped_to_smtp_settings() -> None:
    config = make_settings()

    updated = nacos.apply_remote_settings(
        config,
        """mail:
  host: smtp.example.com
  port: 465
  username: mailer@example.com
  password: remote-password
  default-encoding: UTF-8
  properties:
    mail:
      smtp:
        ssl:
          enable: true
        socketFactory:
          fallback: false
          class: com.example.MailSocketFactory
""",
    )

    assert updated == [
        "SMTP_FROM_EMAIL",
        "SMTP_HOST",
        "SMTP_PASSWORD",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_USE_SSL",
        "SMTP_USE_TLS",
    ]
    assert config.SMTP_HOST == "smtp.example.com"
    assert config.SMTP_PORT == 465
    assert config.SMTP_USERNAME == "mailer@example.com"
    assert config.SMTP_PASSWORD == SecretStr("remote-password")
    assert str(config.SMTP_FROM_EMAIL) == "mailer@example.com"
    assert config.SMTP_USE_TLS is False
    assert config.SMTP_USE_SSL is True


def test_spring_mail_wrapper_and_starttls_are_supported() -> None:
    config = make_settings()

    nacos.apply_remote_settings(
        config,
        """spring:
  mail:
    host: smtp.example.com
    port: 587
    username: mailer@example.com
    password: remote-password
    properties:
      mail:
        smtp:
          starttls:
            required: true
""",
    )

    assert config.SMTP_USE_TLS is True
    assert config.SMTP_USE_SSL is False


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("mail:\n  unknown: value\n", "Unknown Nacos mail settings"),
        (
            "mail:\n  default-encoding: GBK\n",
            "mail.default-encoding must be UTF-8",
        ),
        (
            "mail:\n  host: nested\nSMTP_HOST: flat\n",
            "conflicts with SMTP_HOST",
        ),
        (
            """mail:
  properties:
    mail:
      smtp:
        ssl:
          enable: true
        starttls:
          enable: true
""",
            "SSL and STARTTLS cannot both be enabled",
        ),
    ],
)
def test_spring_style_mail_settings_reject_invalid_payloads(content: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        nacos.apply_remote_settings(make_settings(), content)


def test_remote_storage_config_overrides_local_values() -> None:
    config = make_settings(
        REDIS_URL="redis://local:6379/0",
        POSTGRES_USER="local-user",
        POSTGRES_PASSWORD="local-password",
        POSTGRES_HOST="local-postgres",
        POSTGRES_PORT=5432,
        POSTGRES_DB="local-db",
    )

    nacos.apply_remote_settings(
        config,
        """REDIS_URL: redis://remote:6379/0
POSTGRES_USER: remote-user
POSTGRES_PASSWORD: remote-password
POSTGRES_HOST: remote-postgres
POSTGRES_PORT: 6432
POSTGRES_DB: remote-db
""",
        required_fields=nacos._REMOTE_STORAGE_FIELDS,
    )

    assert config.REDIS_URL == SecretStr("redis://remote:6379/0")
    assert config.POSTGRES_USER == "remote-user"
    assert config.POSTGRES_PASSWORD == SecretStr("remote-password")
    assert config.POSTGRES_HOST == "remote-postgres"
    assert config.POSTGRES_PORT == 6432
    assert config.POSTGRES_DB == "remote-db"


def test_mem0_nested_configuration_and_secret():
    config = make_settings()
    nacos.apply_remote_settings(
        config,
        """memory:
  mem0:
    enabled: true
    collection_name: mem0_chat_test
    api_key: memory-test-key
    base_url: https://models.example/v1
    llm_model: extraction-model
    embedding_model: embedding-model
    embedding_dims: 768
    top_k: 3
    threshold: 0.4
    rerank_model: qwen3.7-text-rerank
    rerank_url: https://example.test/api/v1/services/rerank/text-rerank/text-rerank
    rerank_candidates: 12
    rerank_timeout_seconds: 4
    timeout_seconds: 10
    max_context_chars: 3000
    chat_context_tokens: 8000
""",
    )
    assert config.MEM0_ENABLED
    assert config.MEM0_API_KEY == SecretStr("memory-test-key")
    assert config.MEM0_COLLECTION_NAME == "mem0_chat_test"
    assert config.MEM0_EMBEDDING_DIMS == 768
    assert config.MEM0_LLM_MODEL == "extraction-model"
    assert config.MEM0_TOP_K == 3
    assert config.MEM0_RERANK_MODEL == "qwen3.7-text-rerank"
    assert config.MEM0_RERANK_CANDIDATES == 12
    assert config.MEM0_RERANK_TIMEOUT_SECONDS == 4
    assert config.MEM0_CHAT_CONTEXT_TOKENS == 8000
    assert "memory-test-key" not in repr(config)


@pytest.mark.parametrize("value", ["rag_documents", "mem0_bad-name", "mem0_../../bad"])
def test_mem0_collection_name_rejects_other_tables(value):
    config = make_settings()
    with pytest.raises(ValueError):
        nacos.apply_remote_settings(config, f"MEM0_COLLECTION_NAME: {value}\n")
    assert config.MEM0_COLLECTION_NAME == "mem0_chat"


def test_explicit_cache_setting_maps_from_nacos_and_defaults_off():
    config = make_settings()
    assert config.DASHSCOPE_EXPLICIT_CACHE_ENABLED is False
    nacos.apply_remote_settings(config, "models:\n  dashscope:\n    explicit_cache_enabled: true\n")
    assert config.DASHSCOPE_EXPLICIT_CACHE_ENABLED is True


def test_bailian_search_defaults_on_and_maps_from_nacos():
    config = make_settings()
    assert config.DASHSCOPE_ENABLE_SEARCH is True
    updated = nacos.apply_remote_settings(
        config, "models:\n  dashscope:\n    enable_search: false\n"
    )
    assert updated == ["DASHSCOPE_ENABLE_SEARCH"]
    assert config.DASHSCOPE_ENABLE_SEARCH is False


def test_http_chat_thinking_default_maps_from_nacos():
    config = make_settings()
    assert config.CHAT_LLM_ENABLE_THINKING is False
    nacos.apply_remote_settings(config, "models:\n  chat:\n    enable_thinking: true\n")
    assert config.CHAT_LLM_ENABLE_THINKING is True
