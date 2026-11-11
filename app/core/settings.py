import os
from enum import StrEnum
from json import loads
from pathlib import Path
from tempfile import gettempdir
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values, find_dotenv
from pydantic import (
    BeforeValidator,
    EmailStr,
    Field,
    HttpUrl,
    PrivateAttr,
    SecretStr,
    TypeAdapter,
    computed_field,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.skills import EmailSkill, SkillDefinition
from app.core.speech import validate_speech_configuration
from app.schemas.llm import (
    AlibabaModelName,
    AllModelEnum,
    AnthropicModelName,
    AWSModelName,
    AzureOpenAIModelName,
    DeepseekModelName,
    FakeModelName,
    GoogleModelName,
    GroqModelName,
    OllamaModelName,
    OpenAICompatibleName,
    OpenAIModelName,
    OpenRouterModelName,
    Provider,
    VertexAIModelName,
)
from app.schemas.prompts import Mem0PromptKeys, Mem0Prompts
from app.schemas.voice import VoiceOption, normalize_wake_word


class DatabaseType(StrEnum):
    POSTGRES = "postgres"


class AppEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    def to_logging_level(self) -> int:
        """Convert to Python logging level constant."""
        import logging

        mapping = {
            LogLevel.DEBUG: logging.DEBUG,
            LogLevel.INFO: logging.INFO,
            LogLevel.WARNING: logging.WARNING,
            LogLevel.ERROR: logging.ERROR,
            LogLevel.CRITICAL: logging.CRITICAL,
        }
        return mapping[self]


def check_str_is_http(x: str) -> str:
    http_url_adapter = TypeAdapter(HttpUrl)
    return str(http_url_adapter.validate_python(x))


def project_root() -> Path:
    dotenv_path = find_dotenv(usecwd=True)
    return Path(dotenv_path).parent if dotenv_path else Path.cwd()


def selected_app_environment(dotenv_path: Path | None = None) -> AppEnvironment:
    value = os.getenv("APP_ENV")
    if value is None:
        path = dotenv_path or project_root() / ".env"
        value = dotenv_values(path).get("APP_ENV") if path.is_file() else None

    try:
        return AppEnvironment(value or AppEnvironment.LOCAL)
    except ValueError as exc:
        supported = ", ".join(environment.value for environment in AppEnvironment)
        raise ValueError(f"APP_ENV must be one of: {supported}") from exc


def settings_env_files(
    environment: AppEnvironment | None = None,
    root: Path | None = None,
) -> tuple[Path, ...]:
    root = root or project_root()
    environment = environment or selected_app_environment(root / ".env")
    return (
        root / ".env",
        root / "config" / "environments" / f"{environment.value}.env",
        root / f".env.{environment.value}",
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=settings_env_files(),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        validate_default=False,
    )
    APP_ENV: AppEnvironment = AppEnvironment.LOCAL
    MODE: str | None = None

    HOST: str = "0.0.0.0"
    PORT: int = 8080
    GRACEFUL_SHUTDOWN_TIMEOUT: int = 30
    LOG_LEVEL: LogLevel = LogLevel.WARNING
    LOG_FORMAT: Literal["text", "json"] = "json"

    AUTH_SECRET: SecretStr | None = None
    APP_TOKEN_SECRET: SecretStr | None = None
    APP_TOKEN_TTL_SECONDS: int = Field(default=1_296_000, ge=60, le=1_296_000)
    EMAIL_AUTH_ENABLED: bool = False
    AUTH_SERVICE_ACCOUNT_ID: int = Field(default=0, ge=0)
    REDIS_URL: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    SMTP_HOST: str | None = None
    SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: SecretStr | None = None
    SMTP_FROM_EMAIL: EmailStr | None = None
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False
    EMAIL_SKILL_ENABLED: bool = False
    EMAIL_SKILL_VERSION: str | None = Field(default=None, min_length=1)
    EMAIL_SKILL_LABEL: str = Field(default="latest", min_length=1)
    EMAIL_SKILL_TIMEOUT_SECONDS: float = Field(default=20, ge=1, le=120)
    EMAIL_SKILL_TIMEZONE: str = "Asia/Shanghai"
    _email_skill: EmailSkill | None = PrivateAttr(default=None)

    @field_validator("EMAIL_SKILL_TIMEZONE")
    @classmethod
    def validate_email_skill_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("EMAIL_SKILL_TIMEZONE must be an IANA timezone") from exc
        return value

    @property
    def email_skill(self) -> EmailSkill | None:
        return self._email_skill

    @email_skill.setter
    def email_skill(self, value: EmailSkill | None) -> None:
        self._email_skill = value

    VOICE_ENABLED: bool = False
    VOICE_LLM_ENABLE_THINKING: bool = False
    VOICE_REALTIME_URL: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    VOICE_REALTIME_PROXY: str | None = None
    VOICE_REALTIME_STT_MODEL: str = "qwen3-asr-flash-realtime"
    VOICE_REALTIME_TTS_MODEL: str = "qwen3-tts-flash-realtime"
    VOICE_REALTIME_STT_PROTOCOL: Literal["auto", "realtime", "inference"] = "auto"
    VOICE_REALTIME_TTS_PROTOCOL: Literal["auto", "realtime", "inference"] = "auto"
    VOICE_REALTIME_STT_URL: str | None = None
    VOICE_REALTIME_TTS_URL: str | None = None
    VOICE_REALTIME_VOICES_MODEL: str | None = None
    VOICE_REALTIME_VOICE_CATALOG: dict[str, list[VoiceOption]] = Field(default_factory=dict)
    VOICE_REALTIME_VOICES: list[VoiceOption] = Field(
        default_factory=lambda: [VoiceOption(id="Cherry", name="Cherry")]
    )
    VOICE_MAX_SESSIONS: int = Field(default=8, ge=1, le=1000)
    VOICE_SESSION_SECONDS: int = Field(default=1800, ge=60, le=7200)
    VOICE_IDLE_SECONDS: int = Field(default=120, ge=10, le=600)
    VOICE_UPSTREAM_TIMEOUT: float = Field(default=30, ge=1, le=120)
    VOICE_QUEUE_SIZE: int = Field(default=64, ge=8, le=256)
    VOICE_VAD_SILENCE_MS: int = Field(default=500, ge=200, le=6000)
    VOICE_VAD_THRESHOLD: float = Field(default=0.2, ge=-1, le=1)
    VOICE_WAKE_ENABLED: bool = True
    VOICE_WAKE_WORD: str = Field(default="小美小美", min_length=2, max_length=40)
    VOICE_WAKE_CONFIRM_WITH_ASR: bool = True
    VOICE_WAKE_PRE_ROLL_MS: int = Field(default=4000, ge=400, le=6000)
    VOICE_WAKE_CONFIRM_SECONDS: float = Field(default=10, ge=2, le=30)
    VOICE_WAKE_GREETING: str = Field(
        default="我在，小美随时为你服务。", min_length=1, max_length=100
    )
    VOICE_WAKE_KWS_SCORE: float = Field(default=1.5, ge=0.1, le=10)
    VOICE_WAKE_KWS_THRESHOLD: float = Field(default=0.25, ge=0.01, le=1)
    VOICE_CLIENT_VAD_RMS_DBFS: float = Field(default=-42, ge=-80, le=-10)
    VOICE_CLIENT_VAD_FRAMES: int = Field(default=3, ge=1, le=20)
    VOICE_AUDIO_METRICS_SECONDS: int = Field(default=5, ge=2, le=60)

    @field_validator("VOICE_WAKE_WORD")
    @classmethod
    def validate_wake_word(cls, value: str) -> str:
        keyword = normalize_wake_word(value)
        if len(keyword) < 2:
            raise ValueError("Wake word must contain at least two non-punctuation characters")
        return keyword

    DASHSCOPE_API_KEY: SecretStr | None = None
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DASHSCOPE_EXPLICIT_CACHE_ENABLED: bool = False
    CHAT_LLM_ENABLE_THINKING: bool = False
    DASHSCOPE_EMBEDDING_MODEL: str = "qwen3.7-text-embedding"
    OPENAI_API_KEY: SecretStr | None = None
    DEEPSEEK_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    GOOGLE_APPLICATION_CREDENTIALS: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    USE_AWS_BEDROCK: bool = False
    OLLAMA_MODEL: str | None = None
    OLLAMA_BASE_URL: str | None = None
    USE_FAKE_MODEL: bool = False
    OPENROUTER_API_KEY: SecretStr | None = None

    # If DEFAULT_MODEL is None, it will be set in model_post_init
    DEFAULT_MODEL: AllModelEnum | None = None  # type: ignore[assignment]
    AVAILABLE_MODELS: set[AllModelEnum] = set()  # type: ignore[assignment]

    # Set openai compatible api, mainly used for proof of concept
    COMPATIBLE_MODEL: str | None = None
    COMPATIBLE_API_KEY: SecretStr | None = None
    COMPATIBLE_BASE_URL: str | None = None

    # Accept legacy Nacos configurations without re-enabling removed agents.
    OPENWEATHERMAP_API_KEY: SecretStr | None = None
    MEM0_RESEARCH_ENABLED: bool = False
    MEM0_RESEARCH_TIMEOUT_SECONDS: float = Field(default=90, ge=1, le=120)
    MEM0_RESEARCH_FINAL_TIMEOUT_SECONDS: float = Field(default=15, ge=1, le=60)
    RESEARCH_SEARCH_TIMEOUT_SECONDS: float = Field(default=60, ge=1, le=60)

    # MCP Configuration
    GITHUB_PAT: SecretStr | None = None
    MCP_GITHUB_SERVER_URL: str = "https://api.githubcopilot.com/mcp/"

    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_PROJECT: str = "default"
    LANGCHAIN_ENDPOINT: Annotated[str, BeforeValidator(check_str_is_http)] = (
        "https://api.smith.langchain.com"
    )
    LANGCHAIN_API_KEY: SecretStr | None = None

    LANGFUSE_TRACING: bool = False
    LANGFUSE_HOST: Annotated[str, BeforeValidator(check_str_is_http)] = "https://cloud.langfuse.com"
    LANGFUSE_PUBLIC_KEY: SecretStr | None = None
    LANGFUSE_SECRET_KEY: SecretStr | None = None

    # PostgreSQL is required for both checkpoints and cross-thread memory.
    DATABASE_TYPE: DatabaseType = DatabaseType.POSTGRES

    # PostgreSQL Configuration
    # Local defaults match compose.yaml; containers override the host to postgres.
    POSTGRES_USER: str | None = "postgres"
    POSTGRES_PASSWORD: SecretStr | None = SecretStr("postgres")
    POSTGRES_HOST: str | None = "127.0.0.1"
    POSTGRES_PORT: int | None = 5432
    POSTGRES_DB: str | None = "agent_service"
    POSTGRES_APPLICATION_NAME: str = "agent-service-toolkit"
    POSTGRES_MIN_CONNECTIONS_PER_POOL: int = 1
    POSTGRES_MAX_CONNECTIONS_PER_POOL: int = 1

    RAG_COLLECTION_NAME: str = "acmetech-employee-handbook"
    RAG_TOP_K: int = Field(default=5, ge=1, le=50)

    _mem0_prompts: Mem0Prompts | None = PrivateAttr(default=None)
    MEM0_PROMPT_KEYS: Mem0PromptKeys = Field(default_factory=Mem0PromptKeys)
    MEM0_PROMPT_LABEL: str = Field(default="latest", min_length=1, pattern=r"\S")

    LOCAL_MEMORY_ENABLED: bool = True
    LOCAL_MEMORY_EXTRACTION_TIMEOUT_SECONDS: float = Field(default=20, ge=1, le=120)
    LOCAL_MEMORY_CONTEXT_TIMEOUT_SECONDS: float = Field(default=3, ge=0.1, le=10)

    MEM0_ENABLED: bool = False
    MEM0_COLLECTION_NAME: str = Field(default="mem0_chat", pattern=r"^mem0_[a-z][a-z0-9_]{0,39}$")
    MEM0_LLM_MODEL: str = Field(default="qwen3.8-max", min_length=1)
    MEM0_API_KEY: SecretStr | None = None
    MEM0_BASE_URL: Annotated[str, BeforeValidator(check_str_is_http)] | None = None
    MEM0_EMBEDDING_MODEL: str | None = Field(default=None, min_length=1)
    MEM0_EMBEDDING_DIMS: int = Field(default=1024, ge=1, le=4096)
    MEM0_TOP_K: int = Field(default=5, ge=1, le=50)
    MEM0_THRESHOLD: float = Field(default=0.3, ge=0, le=1)
    MEM0_RERANK_MODEL: str | None = Field(default=None, min_length=1)
    MEM0_RERANK_URL: Annotated[str, BeforeValidator(check_str_is_http)] | None = None
    MEM0_RERANK_CANDIDATES: int = Field(default=20, ge=1, le=100)
    MEM0_RERANK_TIMEOUT_SECONDS: float = Field(default=5, ge=1, le=30)
    MEM0_TIMEOUT_SECONDS: float = Field(default=20, ge=1, le=120)
    MEM0_MAX_CONTEXT_CHARS: int = Field(default=6000, ge=100, le=50000)
    MEM0_CHAT_CONTEXT_TOKENS: int = Field(
        default=12000,
        ge=2048,
        le=131072,
        description="Legacy compatibility setting; no longer limits chat context.",
    )

    QINIU_ACCESS_KEY: SecretStr | None = None
    QINIU_SECRET_KEY: SecretStr | None = None
    QINIU_BUCKET_NAME: str | None = None
    QINIU_PUBLIC_BASE_URL: str | None = None
    QINIU_UPLOAD_TOKEN_TTL_SECONDS: int = Field(default=3600, ge=60, le=86400)
    QINIU_AVATAR_MAX_BYTES: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    FILE_UPLOAD_ENABLED: bool = False
    FILE_UPLOAD_MAX_BYTES: int = Field(default=20 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)

    VIDEO_ENABLED: bool = False
    VIDEO_API_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1"
    VIDEO_SKILL_ENABLED: bool = False
    VIDEO_SKILL_VERSION: str | None = Field(default=None, min_length=1)
    VIDEO_SKILL_LABEL: str = Field(default="latest", min_length=1)
    VIDEO_SKILL_TIMEOUT_SECONDS: float = Field(default=20, ge=1, le=120)
    _video_skill: SkillDefinition | None = PrivateAttr(default=None)

    @property
    def video_skill(self) -> SkillDefinition | None:
        return self._video_skill

    @video_skill.setter
    def video_skill(self, value: SkillDefinition | None) -> None:
        self._video_skill = value

    def require_video_configuration(self) -> None:
        required = (
            "AUTH_SECRET",
            "APP_TOKEN_SECRET",
            "DASHSCOPE_API_KEY",
            "QINIU_ACCESS_KEY",
            "QINIU_SECRET_KEY",
            "QINIU_BUCKET_NAME",
            "QINIU_PUBLIC_BASE_URL",
        )
        missing = [name for name in required if not getattr(self, name)]
        if missing:
            raise ValueError("视频功能缺少配置：" + ", ".join(missing))
        if self.APP_TOKEN_SECRET is None or len(self.APP_TOKEN_SECRET.get_secret_value()) < 32:
            raise ValueError("视频功能要求 APP_TOKEN_SECRET 至少包含 32 个字符")
        endpoint = urlsplit(self.VIDEO_API_BASE_URL)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or not endpoint.hostname.endswith(".aliyuncs.com")
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path.rstrip("/") != "/api/v1"
            or endpoint.port not in (None, 443)
        ):
            raise ValueError("VIDEO_API_BASE_URL 必须是阿里云 HTTPS /api/v1 地址")
        public_url = urlsplit(self.QINIU_PUBLIC_BASE_URL or "")
        if (
            public_url.scheme not in {"https", "http"}
            or not public_url.hostname
            or public_url.username
            or public_url.password
            or public_url.query
            or public_url.fragment
        ):
            raise ValueError("QINIU_PUBLIC_BASE_URL 必须是不含签名参数的公开访问域名")

    # Nacos 3.x service discovery and startup configuration.
    NACOS_ENABLED: bool = False
    NACOS_SERVER_ADDR: str = "127.0.0.1:8848"
    NACOS_CONSOLE_URL: Annotated[str, BeforeValidator(check_str_is_http)] = "http://127.0.0.1:8080/"
    NACOS_CONTEXT_PATH: str = "/nacos"
    NACOS_USERNAME: str | None = None
    NACOS_PASSWORD: SecretStr | None = None
    NACOS_NAMESPACE_ID: str = ""
    NACOS_GROUP_NAME: str = "DEFAULT_GROUP"
    NACOS_CONFIG_DATA_ID: str | None = None
    NACOS_STORAGE_CONFIG_REQUIRED: bool = True
    NACOS_REGISTER_SERVICE: bool = True
    NACOS_SERVICE_NAME: str = "agent-service-toolkit"
    NACOS_SERVICE_IP: str | None = None
    NACOS_SERVICE_PORT: int | None = Field(default=None, ge=1, le=65535)
    NACOS_CLUSTER_NAME: str = "DEFAULT"
    NACOS_METADATA: dict[str, str] = Field(default_factory=dict)
    NACOS_GRPC_PORT_OFFSET: int = Field(default=1000, ge=1, le=65535)
    NACOS_CACHE_DIR: str = f"{gettempdir()}/agent-service-toolkit/nacos/cache"
    NACOS_LOG_DIR: str = f"{gettempdir()}/agent-service-toolkit/nacos/logs"

    # Azure OpenAI Settings
    AZURE_OPENAI_API_KEY: SecretStr | None = None
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_VERSION: str = "2024-02-15-preview"
    AZURE_OPENAI_DEPLOYMENT_MAP: dict[str, str] = Field(
        default_factory=dict, description="Map of model names to Azure deployment IDs"
    )

    @property
    def mem0_prompts(self) -> Mem0Prompts:
        if self._mem0_prompts is None:
            raise RuntimeError("Mem0 prompts must be loaded from Nacos Prompt management")
        return self._mem0_prompts

    @mem0_prompts.setter
    def mem0_prompts(self, value: Mem0Prompts) -> None:
        self._mem0_prompts = value

    def model_post_init(self, __context: Any) -> None:
        has_nacos_password = bool(self.NACOS_PASSWORD and self.NACOS_PASSWORD.get_secret_value())
        if bool(self.NACOS_USERNAME) != has_nacos_password:
            raise ValueError("NACOS_USERNAME and NACOS_PASSWORD must be configured together")

        voice_ids = [voice.id for voice in self.VOICE_REALTIME_VOICES]
        if len(voice_ids) != len(set(voice_ids)):
            raise ValueError("VOICE_REALTIME_VOICES must contain unique voice IDs")

        api_keys = {
            Provider.ALIBABA: self.DASHSCOPE_API_KEY,
            Provider.OPENAI: self.OPENAI_API_KEY,
            Provider.OPENAI_COMPATIBLE: self.COMPATIBLE_BASE_URL and self.COMPATIBLE_MODEL,
            Provider.DEEPSEEK: self.DEEPSEEK_API_KEY,
            Provider.ANTHROPIC: self.ANTHROPIC_API_KEY,
            Provider.GOOGLE: self.GOOGLE_API_KEY,
            Provider.VERTEXAI: self.GOOGLE_APPLICATION_CREDENTIALS,
            Provider.GROQ: self.GROQ_API_KEY,
            Provider.AWS: self.USE_AWS_BEDROCK,
            Provider.OLLAMA: self.OLLAMA_MODEL,
            Provider.FAKE: self.USE_FAKE_MODEL,
            Provider.AZURE_OPENAI: self.AZURE_OPENAI_API_KEY,
            Provider.OPENROUTER: self.OPENROUTER_API_KEY,
        }
        active_keys = [k for k, v in api_keys.items() if v]
        if not active_keys:
            if self.NACOS_ENABLED and self.NACOS_CONFIG_DATA_ID:
                return
            raise ValueError("At least one LLM API key must be provided.")

        # USE_FAKE_MODEL must win the default even when real provider keys are present.
        if self.USE_FAKE_MODEL and self.DEFAULT_MODEL is None:
            self.DEFAULT_MODEL = FakeModelName.FAKE

        for provider in active_keys:
            match provider:
                case Provider.ALIBABA:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AlibabaModelName.QWEN_38_MAX
                    self.AVAILABLE_MODELS.update(set(AlibabaModelName))
                case Provider.OPENAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenAIModelName.GPT_5_NANO
                    self.AVAILABLE_MODELS.update(set(OpenAIModelName))
                case Provider.OPENAI_COMPATIBLE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenAICompatibleName.OPENAI_COMPATIBLE
                    self.AVAILABLE_MODELS.update(set(OpenAICompatibleName))
                case Provider.DEEPSEEK:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = DeepseekModelName.DEEPSEEK_V4_FLASH
                    self.AVAILABLE_MODELS.update(set(DeepseekModelName))
                case Provider.ANTHROPIC:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AnthropicModelName.HAIKU_45
                    self.AVAILABLE_MODELS.update(set(AnthropicModelName))
                case Provider.GOOGLE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = GoogleModelName.GEMINI_36_FLASH
                    self.AVAILABLE_MODELS.update(set(GoogleModelName))
                case Provider.VERTEXAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = VertexAIModelName.GEMINI_36_FLASH
                    self.AVAILABLE_MODELS.update(set(VertexAIModelName))
                case Provider.GROQ:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = GroqModelName.GPT_OSS_20B
                    self.AVAILABLE_MODELS.update(set(GroqModelName))
                case Provider.AWS:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AWSModelName.BEDROCK_HAIKU
                    self.AVAILABLE_MODELS.update(set(AWSModelName))
                case Provider.OLLAMA:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OllamaModelName.OLLAMA_GENERIC
                    self.AVAILABLE_MODELS.update(set(OllamaModelName))
                case Provider.OPENROUTER:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenRouterModelName.GEMINI_36_FLASH
                    self.AVAILABLE_MODELS.update(set(OpenRouterModelName))
                case Provider.FAKE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = FakeModelName.FAKE
                    self.AVAILABLE_MODELS.update(set(FakeModelName))
                case Provider.AZURE_OPENAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AzureOpenAIModelName.AZURE_GPT_5_MINI
                    self.AVAILABLE_MODELS.update(set(AzureOpenAIModelName))
                    # Validate Azure OpenAI settings if Azure provider is available
                    if not self.AZURE_OPENAI_API_KEY:
                        raise ValueError("AZURE_OPENAI_API_KEY must be set")
                    if not self.AZURE_OPENAI_ENDPOINT:
                        raise ValueError("AZURE_OPENAI_ENDPOINT must be set")
                    if not self.AZURE_OPENAI_DEPLOYMENT_MAP:
                        raise ValueError("AZURE_OPENAI_DEPLOYMENT_MAP must be set")

                    # Parse deployment map if it's a string
                    if isinstance(self.AZURE_OPENAI_DEPLOYMENT_MAP, str):
                        try:
                            self.AZURE_OPENAI_DEPLOYMENT_MAP = loads(
                                self.AZURE_OPENAI_DEPLOYMENT_MAP
                            )
                        except Exception as e:
                            raise ValueError(f"Invalid AZURE_OPENAI_DEPLOYMENT_MAP JSON: {e}")

                    # Validate required deployments exist
                    required_models = {"gpt-5", "gpt-5-mini"}
                    missing_models = required_models - set(self.AZURE_OPENAI_DEPLOYMENT_MAP.keys())
                    if missing_models:
                        raise ValueError(f"Missing required Azure deployments: {missing_models}")
                case _:
                    raise ValueError(f"Unknown provider: {provider}")

    def require_model_provider(self) -> None:
        """Reject a resolved runtime configuration without an available model provider."""
        if not self.AVAILABLE_MODELS:
            raise ValueError("At least one LLM API key must be provided.")

    def require_voice_configuration(self) -> None:
        """Reject an enabled realtime voice service without configured voice options."""
        if self.VOICE_ENABLED and not self.VOICE_REALTIME_VOICES:
            raise ValueError("VOICE_REALTIME_VOICES must not be empty when voice is enabled")
        if self.VOICE_ENABLED:
            validate_speech_configuration(self)

    def require_mem0_configuration(self) -> None:
        key = self.MEM0_API_KEY or self.DASHSCOPE_API_KEY
        if self.MEM0_ENABLED and (key is None or not key.get_secret_value().strip()):
            raise ValueError("MEM0_API_KEY or DASHSCOPE_API_KEY is required when MEM0_ENABLED=true")
        if self.MEM0_ENABLED and bool(self.MEM0_RERANK_MODEL) != bool(self.MEM0_RERANK_URL):
            raise ValueError("MEM0_RERANK_MODEL and MEM0_RERANK_URL must be configured together")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def BASE_URL(self) -> str:
        return f"http://{self.HOST}:{self.PORT}"

    def is_dev(self) -> bool:
        return self.MODE == "dev"


settings = Settings()
