import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr, ValidationError

from app.core.settings import (
    AppEnvironment,
    DatabaseType,
    LogLevel,
    Settings,
    check_str_is_http,
    selected_app_environment,
    settings_env_files,
)
from app.schemas.llm import (
    AlibabaModelName,
    AnthropicModelName,
    AzureOpenAIModelName,
    FakeModelName,
    OpenAIModelName,
    OpenRouterModelName,
    VertexAIModelName,
)


def test_check_str_is_http():
    # Test valid HTTP URLs
    assert check_str_is_http("http://example.com/") == "http://example.com/"
    assert check_str_is_http("https://api.test.com/") == "https://api.test.com/"

    # Test invalid URLs
    with pytest.raises(ValidationError):
        check_str_is_http("not_a_url")
    with pytest.raises(ValidationError):
        check_str_is_http("ftp://invalid.com")


def test_local_memory_defaults_and_timeout_bounds():
    settings = Settings(_env_file=None)
    assert settings.LOCAL_MEMORY_ENABLED is True
    assert settings.LOCAL_MEMORY_CONTEXT_TIMEOUT_SECONDS == 3
    assert settings.LOCAL_MEMORY_EXTRACTION_TIMEOUT_SECONDS == 20
    for fields in [
        {"LOCAL_MEMORY_CONTEXT_TIMEOUT_SECONDS": 0},
        {"LOCAL_MEMORY_EXTRACTION_TIMEOUT_SECONDS": 0},
    ]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **fields)


def test_settings_default_values():
    settings = Settings(_env_file=None)
    assert settings.HOST == "0.0.0.0"
    assert settings.PORT == 8080
    assert settings.USE_AWS_BEDROCK is False
    assert settings.USE_FAKE_MODEL is False
    assert settings.EMAIL_AUTH_ENABLED is False
    assert settings.APP_TOKEN_TTL_SECONDS == 1_296_000
    assert settings.REDIS_URL == SecretStr("redis://127.0.0.1:6379/0")
    assert settings.SMTP_PORT == 587
    assert settings.SMTP_USE_SSL is False
    assert settings.NACOS_ENABLED is False
    assert settings.NACOS_SERVER_ADDR == "127.0.0.1:8848"
    assert settings.NACOS_CONSOLE_URL == "http://127.0.0.1:8080/"
    assert settings.NACOS_SERVICE_NAME == "agent-service-toolkit"
    assert [voice.id for voice in settings.VOICE_REALTIME_VOICES] == ["Cherry"]
    assert settings.VOICE_WAKE_WORD == "小美小美"
    assert settings.VOICE_WAKE_PRE_ROLL_MS == 4000
    assert settings.VOICE_WAKE_KWS_SCORE == 1.5
    assert settings.VOICE_WAKE_KWS_THRESHOLD == 0.25


@pytest.mark.parametrize("keyword", ["小美", "小美小美", "小美，小美", "小美， 小美。"])
def test_wake_word_configuration_accepts_legacy_and_punctuated_phrases(keyword):
    config = Settings(_env_file=None, VOICE_WAKE_WORD=keyword)
    assert config.VOICE_WAKE_WORD == "小美小美"


def test_app_token_ttl_is_limited_to_fifteen_days():
    with pytest.raises(ValidationError, match="APP_TOKEN_TTL_SECONDS"):
        Settings(_env_file=None, APP_TOKEN_TTL_SECONDS=1_296_001)


def test_email_timezone_must_be_valid():
    assert (
        Settings(_env_file=None, EMAIL_SKILL_TIMEZONE="Asia/Tokyo").EMAIL_SKILL_TIMEZONE
        == "Asia/Tokyo"
    )
    with pytest.raises(ValidationError, match="EMAIL_SKILL_TIMEZONE"):
        Settings(_env_file=None, EMAIL_SKILL_TIMEZONE="invalid-zone")


@pytest.mark.parametrize("budget", [0, 2047, 131073])
def test_mem0_chat_budget_rejects_invalid_limits(budget):
    with pytest.raises(ValidationError, match="MEM0_CHAT_CONTEXT_TOKENS"):
        Settings(_env_file=None, MEM0_CHAT_CONTEXT_TOKENS=budget)


@pytest.mark.parametrize(
    "name,maximum",
    [
        ("MEM0_RESEARCH_TIMEOUT_SECONDS", 120),
        ("MEM0_RESEARCH_FINAL_TIMEOUT_SECONDS", 60),
        ("RESEARCH_SEARCH_TIMEOUT_SECONDS", 60),
    ],
)
@pytest.mark.parametrize("invalid", [0, -1, 1000])
def test_research_deadlines_reject_invalid_values(name, maximum, invalid):
    with pytest.raises(ValidationError, match=name):
        Settings(_env_file=None, **{name: invalid})
    with pytest.raises(ValidationError, match=name):
        Settings(_env_file=None, **{name: maximum + 1})
    assert getattr(Settings(_env_file=None, **{name: maximum}), name) == maximum


def test_research_defaults_and_environment_override(monkeypatch):
    defaults = Settings(_env_file=None)
    assert not defaults.MEM0_RESEARCH_ENABLED
    assert defaults.MEM0_RESEARCH_TIMEOUT_SECONDS == 90
    assert defaults.MEM0_RESEARCH_FINAL_TIMEOUT_SECONDS == 15
    assert defaults.RESEARCH_SEARCH_TIMEOUT_SECONDS == 60
    monkeypatch.setenv("MEM0_RESEARCH_ENABLED", "false")
    monkeypatch.setenv("MEM0_RESEARCH_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("RESEARCH_SEARCH_TIMEOUT_SECONDS", "7")
    config = Settings(_env_file=None)
    assert not config.MEM0_RESEARCH_ENABLED and config.MEM0_RESEARCH_TIMEOUT_SECONDS == 10
    assert config.RESEARCH_SEARCH_TIMEOUT_SECONDS == 7


def test_mem0_reranker_requires_model_and_url_together():
    from pydantic import SecretStr

    for options in (
        {"MEM0_RERANK_MODEL": "qwen3.7-text-rerank"},
        {"MEM0_RERANK_URL": "https://example.test/rerank"},
    ):
        config = Settings(
            _env_file=None,
            USE_FAKE_MODEL=True,
            MEM0_ENABLED=True,
            MEM0_API_KEY=SecretStr("test"),
            **options,
        )
        with pytest.raises(ValueError, match="MEM0_RERANK_MODEL and MEM0_RERANK_URL"):
            config.require_mem0_configuration()

    config = Settings(
        _env_file=None,
        USE_FAKE_MODEL=True,
        MEM0_ENABLED=True,
        MEM0_API_KEY=SecretStr("test"),
        MEM0_RERANK_MODEL="qwen3.7-text-rerank",
        MEM0_RERANK_URL="https://example.test/rerank",
    )
    config.require_mem0_configuration()


def test_selected_app_environment_uses_dotenv_and_process_override(tmp_path: Path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("APP_ENV=test\n", encoding="utf-8")

    with patch.dict(os.environ, {}, clear=True):
        assert selected_app_environment(dotenv_path) == AppEnvironment.TEST

    with patch.dict(os.environ, {"APP_ENV": "local"}, clear=True):
        assert selected_app_environment(dotenv_path) == AppEnvironment.LOCAL


def test_environment_files_apply_profile_and_secret_overrides(tmp_path: Path):
    environment_dir = tmp_path / "config" / "environments"
    environment_dir.mkdir(parents=True)
    (tmp_path / ".env").write_text("APP_ENV=test\nUSE_FAKE_MODEL=true\n", encoding="utf-8")
    (environment_dir / "test.env").write_text(
        "APP_ENV=test\nNACOS_SERVER_ADDR=124.221.238.140:8848\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.test").write_text(
        "NACOS_USERNAME=nacos\nNACOS_PASSWORD=test-secret\n",
        encoding="utf-8",
    )

    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=settings_env_files(AppEnvironment.TEST, tmp_path),
        )

    assert settings.APP_ENV == AppEnvironment.TEST
    assert settings.NACOS_SERVER_ADDR == "124.221.238.140:8848"
    assert settings.NACOS_USERNAME == "nacos"
    assert settings.NACOS_PASSWORD == SecretStr("test-secret")


def test_selected_app_environment_rejects_unknown_value(tmp_path: Path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("APP_ENV=staging\n", encoding="utf-8")

    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="APP_ENV must be one of: local, test"):
            selected_app_environment(dotenv_path)


@pytest.mark.parametrize(
    ("environment", "server_addr", "console_url", "mode", "port"),
    [
        (AppEnvironment.LOCAL, "127.0.0.1:8848", "http://127.0.0.1:8080/", "dev", 8000),
        (
            AppEnvironment.TEST,
            "124.221.238.140:8848",
            "http://124.221.238.140:8080/",
            "prod",
            8000,
        ),
    ],
)
def test_committed_environment_profiles(
    environment: AppEnvironment,
    server_addr: str,
    console_url: str,
    mode: str,
    port: int,
):
    root = Path(__file__).parents[2]

    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=(root / "config" / "environments" / f"{environment.value}.env",),
        )

    assert settings.APP_ENV == environment
    assert settings.MODE == mode
    assert settings.PORT == port
    assert settings.NACOS_SERVER_ADDR == server_addr
    assert settings.NACOS_CONSOLE_URL == console_url


def test_test_environment_template_does_not_register_local_instance():
    root = Path(__file__).parents[2]

    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=(
                root / "config" / "environments" / "test.env",
                root / ".env.test.example",
            ),
        )

    assert settings.APP_ENV == AppEnvironment.TEST
    assert settings.NACOS_REGISTER_SERVICE is False
    assert settings.PORT == 8000


def test_settings_accepts_legacy_and_structured_voice_options():
    settings = Settings(
        _env_file=None,
        USE_FAKE_MODEL=True,
        VOICE_ENABLED=True,
        VOICE_REALTIME_VOICES=[
            "Cherry",
            {"id": "Serena", "name": "苏瑶", "description": "温柔自然"},
        ],
    )

    assert [voice.id for voice in settings.VOICE_REALTIME_VOICES] == ["Cherry", "Serena"]
    assert settings.VOICE_REALTIME_VOICES[0].name == "Cherry"
    assert settings.VOICE_REALTIME_VOICES[1].name == "苏瑶"


def test_voice_configuration_requires_unique_nonempty_options_when_enabled():
    with pytest.raises(ValueError, match="must not be empty"):
        Settings(
            _env_file=None,
            USE_FAKE_MODEL=True,
            VOICE_ENABLED=True,
            VOICE_REALTIME_VOICES=[],
        ).require_voice_configuration()

    with pytest.raises(ValidationError, match="unique voice IDs"):
        Settings(
            _env_file=None,
            USE_FAKE_MODEL=True,
            VOICE_REALTIME_VOICES=["Cherry", "Cherry"],
        )


def test_nacos_credentials_must_be_configured_together():
    with pytest.raises(ValidationError, match="NACOS_USERNAME and NACOS_PASSWORD"):
        Settings(_env_file=None, NACOS_USERNAME="nacos")


def test_nacos_password_is_redacted():
    settings = Settings(
        _env_file=None,
        NACOS_USERNAME="nacos",
        NACOS_PASSWORD="test-nacos-password",
    )
    assert "test-nacos-password" not in repr(settings)


def test_local_storage_defaults_match_compose_postgres():
    with patch.dict(os.environ, {"USE_FAKE_MODEL": "true"}, clear=True):
        settings = Settings(_env_file=None)
    assert settings.DATABASE_TYPE == DatabaseType.POSTGRES
    assert settings.POSTGRES_HOST == "127.0.0.1"
    assert settings.POSTGRES_PORT == 5432
    assert settings.POSTGRES_DB == "agent_service"
    assert settings.POSTGRES_USER == "postgres"
    assert settings.POSTGRES_PASSWORD == SecretStr("postgres")


@pytest.mark.parametrize("backend", ["sqlite", "mongo", "memory"])
def test_non_postgres_backend_is_rejected(backend):
    with patch.dict(os.environ, {"USE_FAKE_MODEL": "true", "DATABASE_TYPE": backend}, clear=True):
        with pytest.raises(ValidationError, match="DATABASE_TYPE"):
            Settings(_env_file=None)


def test_settings_no_api_keys():
    # Test that settings raises error when no API keys are provided
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="At least one LLM API key must be provided"):
            _ = Settings(_env_file=None)


def test_settings_with_openai_key():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.OPENAI_API_KEY == SecretStr("test_key")
        assert settings.DEFAULT_MODEL == OpenAIModelName.GPT_5_NANO
        assert settings.AVAILABLE_MODELS == set(OpenAIModelName)


def test_settings_with_dashscope_key():
    with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.DASHSCOPE_API_KEY == SecretStr("test_key")
        assert settings.DASHSCOPE_BASE_URL == ("https://dashscope.aliyuncs.com/compatible-mode/v1")
        assert settings.DASHSCOPE_EMBEDDING_MODEL == "qwen3.7-text-embedding"
        assert settings.VOICE_LLM_ENABLE_THINKING is False
        assert settings.DEFAULT_MODEL == AlibabaModelName.QWEN_38_MAX
        assert settings.AVAILABLE_MODELS == set(AlibabaModelName)


def test_settings_dashscope_is_default_with_multiple_api_keys():
    with patch.dict(
        os.environ,
        {
            "DASHSCOPE_API_KEY": "test_dashscope_key",
            "OPENAI_API_KEY": "test_openai_key",
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.DEFAULT_MODEL == AlibabaModelName.QWEN_38_MAX
        assert settings.AVAILABLE_MODELS == set(AlibabaModelName) | set(OpenAIModelName)


def test_settings_with_anthropic_key():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.ANTHROPIC_API_KEY == SecretStr("test_key")
        assert settings.DEFAULT_MODEL == AnthropicModelName.HAIKU_45
        assert settings.AVAILABLE_MODELS == set(AnthropicModelName)


def test_settings_with_openrouter_key():
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.OPENROUTER_API_KEY == SecretStr("test_key")
        assert settings.DEFAULT_MODEL == OpenRouterModelName.GEMINI_36_FLASH
        assert settings.AVAILABLE_MODELS == set(OpenRouterModelName)
        # SecretStr keeps the key out of logs and tracebacks that render Settings.
        assert "test_key" not in repr(settings)


def test_settings_with_vertexai_credentials_file():
    with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.GOOGLE_APPLICATION_CREDENTIALS == SecretStr("test_key")
        assert settings.DEFAULT_MODEL == VertexAIModelName.GEMINI_36_FLASH
        assert settings.AVAILABLE_MODELS == set(VertexAIModelName)


def test_settings_with_multiple_api_keys():
    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "test_openai_key",
            "ANTHROPIC_API_KEY": "test_anthropic_key",
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.OPENAI_API_KEY == SecretStr("test_openai_key")
        assert settings.ANTHROPIC_API_KEY == SecretStr("test_anthropic_key")
        # When multiple providers are available, OpenAI should be the default
        assert settings.DEFAULT_MODEL == OpenAIModelName.GPT_5_NANO
        # Available models should include exactly all OpenAI and Anthropic models
        expected_models = set(OpenAIModelName)
        expected_models.update(set(AnthropicModelName))
        assert settings.AVAILABLE_MODELS == expected_models


def test_settings_use_fake_model():
    with patch.dict(os.environ, {"USE_FAKE_MODEL": "true"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.DEFAULT_MODEL == FakeModelName.FAKE
        assert settings.AVAILABLE_MODELS == set(FakeModelName)


def test_settings_use_fake_model_wins_over_ambient_real_keys():
    # USE_FAKE_MODEL must win the default even when real provider keys are present.
    with patch.dict(
        os.environ,
        {
            "USE_FAKE_MODEL": "true",
            "OPENAI_API_KEY": "test_openai_key",
            "GROQ_API_KEY": "test_groq_key",
            "GOOGLE_API_KEY": "test_google_key",
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.DEFAULT_MODEL == FakeModelName.FAKE
        # AVAILABLE_MODELS still unions every active provider.
        assert set(FakeModelName).issubset(settings.AVAILABLE_MODELS)
        assert set(OpenAIModelName).issubset(settings.AVAILABLE_MODELS)


def test_settings_explicit_default_model_overrides_fake():
    # An explicit DEFAULT_MODEL takes precedence over USE_FAKE_MODEL.
    with patch.dict(
        os.environ,
        {"USE_FAKE_MODEL": "true", "OPENAI_API_KEY": "test_openai_key"},
        clear=True,
    ):
        settings = Settings(_env_file=None, DEFAULT_MODEL=OpenAIModelName.GPT_5_NANO)
        assert settings.DEFAULT_MODEL == OpenAIModelName.GPT_5_NANO


def test_settings_base_url():
    settings = Settings(HOST="0.0.0.0", PORT=8000, _env_file=None)
    assert settings.BASE_URL == "http://0.0.0.0:8000"


def test_settings_is_dev():
    settings = Settings(MODE="dev", _env_file=None)
    assert settings.is_dev() is True

    settings = Settings(MODE="prod", _env_file=None)
    assert settings.is_dev() is False


def test_settings_with_azure_openai_key():
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": '{"gpt-5": "deployment-1", "gpt-5-mini": "deployment-2"}',
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.AZURE_OPENAI_API_KEY.get_secret_value() == "test_key"
        assert settings.DEFAULT_MODEL == AzureOpenAIModelName.AZURE_GPT_5_MINI
        assert settings.AVAILABLE_MODELS == set(AzureOpenAIModelName)


def test_settings_with_both_openai_and_azure():
    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "test_openai_key",
            "AZURE_OPENAI_API_KEY": "test_azure_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": '{"gpt-5": "deployment-1", "gpt-5-mini": "deployment-2"}',
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.OPENAI_API_KEY == SecretStr("test_openai_key")
        assert settings.AZURE_OPENAI_API_KEY == SecretStr("test_azure_key")
        # When multiple providers are available, OpenAI should be the default
        assert settings.DEFAULT_MODEL == OpenAIModelName.GPT_5_NANO
        # Available models should include both OpenAI and Azure OpenAI models
        expected_models = set(OpenAIModelName)
        expected_models.update(set(AzureOpenAIModelName))
        assert settings.AVAILABLE_MODELS == expected_models


def test_settings_azure_deployment_names():
    # Delete this test
    pass


def test_settings_azure_missing_deployment_names():
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        },
        clear=True,
    ):
        with pytest.raises(ValidationError, match="AZURE_OPENAI_DEPLOYMENT_MAP must be set"):
            Settings(_env_file=None)


def test_settings_azure_deployment_map():
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": '{"gpt-5": "deploy1", "gpt-5-mini": "deploy2"}',
        },
        clear=True,
    ):
        settings = Settings(_env_file=None)
        assert settings.AZURE_OPENAI_DEPLOYMENT_MAP == {
            "gpt-5": "deploy1",
            "gpt-5-mini": "deploy2",
        }


def test_settings_azure_invalid_deployment_map():
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test_key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": '{"gpt-5": "deploy1"}',  # Missing required model
        },
        clear=True,
    ):
        with pytest.raises(ValueError, match="Missing required Azure deployments"):
            Settings(_env_file=None)


def test_settings_azure_openai():
    """Test Azure OpenAI settings."""
    deployment_map = {"gpt-5": "deployment1", "gpt-5-mini": "deployment2"}
    with patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_API_KEY": "test-key",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_MAP": json.dumps(deployment_map),
        },
    ):
        settings = Settings(_env_file=None)
        assert settings.AZURE_OPENAI_API_KEY.get_secret_value() == "test-key"
        assert settings.AZURE_OPENAI_ENDPOINT == "https://test.openai.azure.com"
        assert settings.AZURE_OPENAI_DEPLOYMENT_MAP == deployment_map


def test_log_level_enum():
    """Test LogLevel enum and its conversion to logging levels."""
    assert LogLevel.DEBUG.to_logging_level() == logging.DEBUG
    assert LogLevel.INFO.to_logging_level() == logging.INFO
    assert LogLevel.WARNING.to_logging_level() == logging.WARNING
    assert LogLevel.ERROR.to_logging_level() == logging.ERROR
    assert LogLevel.CRITICAL.to_logging_level() == logging.CRITICAL


def test_settings_log_level_default():
    """Test that LOG_LEVEL defaults to WARNING."""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test_key"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.LOG_LEVEL == LogLevel.WARNING
        assert settings.LOG_LEVEL.to_logging_level() == logging.WARNING


def test_settings_log_level_from_env():
    """Test that LOG_LEVEL can be set from environment variable."""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test_key", "LOG_LEVEL": "DEBUG"}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.LOG_LEVEL == LogLevel.DEBUG
        assert settings.LOG_LEVEL.to_logging_level() == logging.DEBUG


def test_settings_log_level_invalid():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test_key", "LOG_LEVEL": "INVALID"}, clear=True):
        with pytest.raises(ValueError, match="validation error for Settings\nLOG_LEVEL\n"):
            Settings(_env_file=None)
