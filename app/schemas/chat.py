from datetime import date, datetime
from typing import Any, Literal, NotRequired

from pydantic import BaseModel, Field, SerializeAsAny
from typing_extensions import TypedDict

from app.schemas.llm import AlibabaModelName, AllModelEnum, AnthropicModelName
from app.schemas.local_memory import LocalMemoryContext


class AgentInfo(BaseModel):
    """Info about an available agent."""

    key: str = Field(
        description="Agent key.",
        examples=["research-assistant"],
    )
    description: str = Field(
        description="Description of the agent.",
        examples=["A research assistant for generating research papers."],
    )


class ServiceMetadata(BaseModel):
    """Metadata about the service including available agents and models."""

    agents: list[AgentInfo] = Field(
        description="List of available agents.",
    )
    models: list[AllModelEnum] = Field(
        description="List of available LLMs.",
    )
    default_agent: str = Field(
        description="Default agent used when none is specified.",
        examples=["research-assistant"],
    )
    default_model: AllModelEnum = Field(
        description="Default model used when none is specified.",
    )


class UserInput(BaseModel):
    """Basic user input for the agent."""

    local_memory: LocalMemoryContext | None = None

    message: str = Field(
        description="User input to the agent.",
        examples=["What is the weather in Tokyo?"],
    )
    model: SerializeAsAny[AllModelEnum] | None = Field(
        title="Model",
        description="LLM Model to use for the agent. Defaults to the default model set in the settings of the service.",
        default=None,
        examples=[AlibabaModelName.QWEN_38_MAX, AnthropicModelName.HAIKU_45],
    )
    thread_id: str | None = Field(
        description="Thread ID to persist and continue a multi-turn conversation.",
        default=None,
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    user_id: str | None = Field(
        description="User ID to persist and continue a conversation across multiple threads.",
        default=None,
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    agent_config: dict[str, Any] = Field(
        description="Additional configuration to pass through to the agent",
        default={},
        examples=[{"spicy_level": 0.8}],
    )


class StreamInput(UserInput):
    """User input for streaming the agent's response."""

    stream_tokens: bool = Field(
        description="Whether to stream LLM tokens to the client.",
        default=True,
    )


class ToolCall(TypedDict):
    """Represents a request to call a tool."""

    name: str
    """The name of the tool to be called."""
    args: dict[str, Any]
    """The arguments to the tool call."""
    id: str | None
    """An identifier associated with the tool call."""
    type: NotRequired[Literal["tool_call"]]


class ChatMessage(BaseModel):
    """Message in a chat."""

    type: Literal["human", "ai", "tool", "custom"] = Field(
        description="Role of the message.",
        examples=["human", "ai", "tool", "custom"],
    )
    content: str = Field(
        description="Content of the message.",
        examples=["Hello, world!"],
    )
    tool_calls: list[ToolCall] = Field(
        description="Tool calls in the message.",
        default=[],
    )
    tool_call_id: str | None = Field(
        description="Tool call that this message is responding to.",
        default=None,
        examples=["call_Jja7J89XsjrOLA5r!MEOW!SL"],
    )
    run_id: str | None = Field(
        description="Run ID of the message.",
        default=None,
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    response_metadata: dict[str, Any] = Field(
        description="Response metadata. For example: response headers, logprobs, token counts.",
        default={},
    )
    custom_data: dict[str, Any] = Field(
        description="Custom message data.",
        default={},
    )

    def pretty_repr(self) -> str:
        """Get a pretty representation of the message."""
        base_title = self.type.title() + " Message"
        padded = " " + base_title + " "
        sep_len = (80 - len(padded)) // 2
        sep = "=" * sep_len
        second_sep = sep + "=" if len(padded) % 2 else sep
        title = f"{sep}{padded}{second_sep}"
        return f"{title}\n\n{self.content}"

    def pretty_print(self) -> None:
        print(self.pretty_repr())  # noqa: T201


class Feedback(BaseModel):  # type: ignore[no-redef]
    """Feedback for a run, to record to LangSmith."""

    run_id: str = Field(
        description="Run ID to record feedback for.",
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    key: str = Field(
        description="Feedback key.",
        examples=["human-feedback-stars"],
    )
    score: float = Field(
        description="Feedback score.",
        examples=[0.8],
    )
    kwargs: dict[str, Any] = Field(
        description="Additional feedback kwargs, passed to LangSmith.",
        default={},
        examples=[{"comment": "In-line human feedback"}],
    )


class FeedbackResponse(BaseModel):
    status: Literal["success"] = "success"


class ChatHistoryInput(BaseModel):
    """Input for retrieving chat history."""

    thread_id: str = Field(
        description="Thread ID to persist and continue a multi-turn conversation.",
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )


class ChatHistory(BaseModel):
    messages: list[ChatMessage]


class UserThreadsInput(BaseModel):
    """Input for listing a user's conversation threads."""

    user_id: str = Field(
        description="User ID to list threads for.",
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    limit: int = Field(
        description="Maximum number of threads to return.",
        default=20,
        ge=1,
        le=100,
    )


class ThreadSummary(BaseModel):
    """Summary of a single conversation thread."""

    thread_id: str = Field(
        description="Thread ID of the conversation.",
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    agent_id: str = Field(
        description="Agent this thread was run with.",
        examples=["research-assistant"],
    )
    updated_at: datetime | None = Field(
        description="Timestamp of the most recent checkpoint in this thread.",
        default=None,
        examples=["2024-07-31T20:14:19.804150+00:00"],
    )
    title: str | None = Field(
        description="Title for the thread, derived from the first human message.",
        default=None,
        examples=["What is the weather in Tokyo?"],
    )


class UserThreads(BaseModel):
    threads: list[ThreadSummary]


class ChatRecordsInput(BaseModel):
    """Input for listing or retrieving a user's daily chat records."""

    user_id: str = Field(
        description="User ID whose chat records should be returned.",
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )


class ChatRecordDay(BaseModel):
    """Summary of chat activity on one UTC calendar day."""

    record_date: date = Field(
        description="UTC calendar date containing chat activity.",
        examples=["2026-10-13"],
    )
    latest_at: datetime = Field(
        description="Timestamp of the latest chat activity on this date.",
        examples=["2026-10-13T08:30:00+00:00"],
    )
    conversation_count: int = Field(
        description="Number of conversations with activity on this date.",
        ge=1,
    )


class ChatRecordList(BaseModel):
    """Daily chat activity from the latest 30 UTC calendar days."""

    records: list[ChatRecordDay]


class DailyChatConversation(BaseModel):
    """Messages added to a conversation on one UTC calendar day."""

    thread_id: str = Field(description="Thread ID of the conversation.")
    agent_id: str = Field(description="Agent used by the conversation.")
    updated_at: datetime = Field(description="Latest activity timestamp on this date.")
    title: str | None = Field(
        description="Conversation title derived from its first human message.",
        default=None,
    )
    messages: list[ChatMessage] = Field(
        description="Messages added to the conversation on this date."
    )


class ChatRecordDetail(BaseModel):
    """Conversation content for one UTC calendar day."""

    record_date: date
    conversations: list[DailyChatConversation]
