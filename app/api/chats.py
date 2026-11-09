import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langsmith import Client as LangsmithClient

from app.agents import DEFAULT_AGENT, AgentGraph, get_all_agent_info
from app.api.dependencies import get_agent, repository
from app.core import settings
from app.repositories.voice import VoiceRepository
from app.schemas import (
    ApiResponse,
    ChatHistory,
    ChatHistoryInput,
    ChatMessage,
    ChatRecordDetail,
    ChatRecordList,
    ChatRecordsInput,
    Feedback,
    FeedbackResponse,
    ServiceMetadata,
    StreamInput,
    UserInput,
    UserThreads,
    UserThreadsInput,
    api_success,
)
from app.schemas.execution import ExecutionList
from app.schemas.memory_commands import PendingMemoryCommand
from app.services.agent_runner import graph_events, thread_guard
from app.services.auth import authorize_thread, bind_user
from app.services.chat import build_run_input, invoke_agent
from app.services.chat_records import (
    get_chat_record_detail,
    list_chat_record_days,
    recent_record_dates,
)
from app.services.execution import confirmation_id, execution_history
from app.services.messages import langchain_to_chat_message, messages_from_checkpoint
from app.services.threads import list_user_threads

logger = logging.getLogger(__name__)


router = APIRouter()


@router.get("/info", response_model=ApiResponse[ServiceMetadata])
async def info() -> ApiResponse[ServiceMetadata]:
    models = list(settings.AVAILABLE_MODELS)
    models.sort()
    return api_success(
        ServiceMetadata(
            agents=get_all_agent_info(),
            models=models,
            default_agent=DEFAULT_AGENT,
            default_model=settings.DEFAULT_MODEL,
        )
    )


@router.post("/{agent_id}/invoke", operation_id="invoke_with_agent_id")
@router.post("/invoke")
async def invoke(
    user_input: UserInput, request: Request, agent_id: str = DEFAULT_AGENT
) -> ApiResponse[ChatMessage]:
    repo = repository(request)
    identity = request.state.principal
    user_input.user_id = bind_user(identity, user_input.user_id)
    user_input.thread_id = user_input.thread_id or str(uuid4())
    agent = get_agent(agent_id)
    async with thread_guard(repo, user_input.thread_id):
        await authorize_thread(
            repo, agent, user_input.thread_id, agent_id, identity, user_input.user_id, create=True
        )
        return api_success(
            await invoke_agent(
                user_input, agent, agent_id, repo, authenticated_user_id=identity.user_id
            )
        )


async def message_generator(
    user_input: StreamInput,
    agent_id: str = DEFAULT_AGENT,
    repo: VoiceRepository | None = None,
    *,
    authenticated_user_id: str | None = None,
) -> AsyncGenerator[str, None]:
    agent = get_agent(agent_id)
    user_input.thread_id = user_input.thread_id or str(uuid4())
    try:
        async with thread_guard(repo, user_input.thread_id):
            kwargs, run_id = await build_run_input(
                user_input, agent, agent_id, repo, authenticated_user_id=authenticated_user_id
            )
            async for event in graph_events(
                agent,
                kwargs,
                str(run_id),
                stream_tokens=user_input.stream_tokens,
                user_message=user_input.message,
            ):
                if event.type == "interrupt":
                    content = event.content["value"]
                    if not isinstance(content, str):
                        content = json.dumps(content, ensure_ascii=False)
                    message = ChatMessage(type="ai", content=content, run_id=str(run_id))
                    payload = {"type": "message", "content": message.model_dump()}
                elif event.type == "message":
                    payload = {"type": "message", "content": event.content.model_dump()}
                else:
                    payload = {"type": event.type, "content": event.content}
                yield f"data: {json.dumps(payload)}\n\n"
    except Exception:
        logger.exception("Error in message generator")
        yield f"data: {json.dumps({'type': 'error', 'content': 'Internal server error'})}\n\n"
    yield "data: [DONE]\n\n"


@router.get("/{agent_id}/executions", operation_id="executions_with_agent_id")
@router.get("/executions")
async def executions(
    request: Request,
    thread_id: str = Query(min_length=1, max_length=128),
    user_id: str | None = None,
    agent_id: str = DEFAULT_AGENT,
) -> ApiResponse[ExecutionList]:
    agent = get_agent(agent_id)
    repo = repository(request)
    identity = request.state.principal
    if not identity.admin:
        bind_user(identity, user_id)
    await authorize_thread(repo, agent, thread_id, agent_id, identity)
    owner = await repo.owner(thread_id) if repo else None
    bound_user = owner[0] if owner else bind_user(identity, user_id)
    snapshots = await execution_history(getattr(agent, "store", None), bound_user, thread_id)
    if repo:
        turns = {turn.response_id: turn for turn in await repo.turns(thread_id)}
        for snapshot in snapshots:
            turn = turns.get(snapshot.response_id) if snapshot.response_id else None
            if turn and not snapshot.text:
                snapshot.text = turn.generated_text
    saver = getattr(agent, "checkpointer", None)
    checkpoint = (
        await saver.aget_tuple(RunnableConfig(configurable={"thread_id": thread_id}))
        if saver
        else None
    )
    previous = (
        ((checkpoint.checkpoint.get("channel_values") or {}).get("__previous__") or {})
        if checkpoint
        else {}
    )
    pending = (
        (previous.get("memory_dialogue") or {}).get("pending")
        if isinstance(previous, dict)
        else None
    )
    current_id = confirmation_id(PendingMemoryCommand.model_validate(pending)) if pending else None
    checkpoint_at = datetime.fromisoformat(checkpoint.checkpoint["ts"]) if checkpoint else None
    for snapshot in snapshots:
        if (
            snapshot.confirmation
            and snapshot.confirmation.confirmation_id != current_id
            and checkpoint_at is not None
            and snapshot.updated_at <= checkpoint_at
        ):
            snapshot.confirmation = None
            if snapshot.status == "waiting_confirmation":
                snapshot.status = "cancelled"
                for step in snapshot.steps:
                    if step.status == "waiting_confirmation":
                        step.status = "cancelled"
                        step.detail = "该确认已处理或被后续请求替代，请查看后续执行结果"
    return api_success(ExecutionList(executions=snapshots))


def _sse_response_example() -> dict[int | str, Any]:
    return {
        status.HTTP_200_OK: {
            "description": "Server Sent Event Response",
            "content": {
                "text/event-stream": {
                    "example": "data: {'type': 'token', 'content': 'Hello'}\n\ndata: {'type': 'token', 'content': ' World'}\n\ndata: [DONE]\n\n",
                    "schema": {"type": "string"},
                }
            },
        }
    }


@router.post(
    "/{agent_id}/stream",
    response_class=StreamingResponse,
    responses=_sse_response_example(),
    operation_id="stream_with_agent_id",
)
@router.post("/stream", response_class=StreamingResponse, responses=_sse_response_example())
async def stream(
    user_input: StreamInput, request: Request, agent_id: str = DEFAULT_AGENT
) -> StreamingResponse:
    """
    Stream an agent's response to a user input, including intermediate messages and tokens.

    If agent_id is not provided, the default agent will be used.
    Use thread_id to persist and continue a multi-turn conversation. run_id kwarg
    is also attached to all messages for recording feedback.
    Use user_id to persist and continue a conversation across multiple threads.

    Set `stream_tokens=false` to return intermediate messages but not token-by-token.
    """
    repo = repository(request)
    identity = request.state.principal
    user_input.user_id = bind_user(identity, user_input.user_id)
    user_input.thread_id = user_input.thread_id or str(uuid4())
    await authorize_thread(
        repo,
        get_agent(agent_id),
        user_input.thread_id,
        agent_id,
        identity,
        user_input.user_id,
        create=True,
    )
    return StreamingResponse(
        message_generator(user_input, agent_id, repo, authenticated_user_id=identity.user_id),
        media_type="text/event-stream",
    )


@router.post("/feedback", response_model=ApiResponse[FeedbackResponse])
async def feedback(feedback: Feedback, request: Request) -> ApiResponse[FeedbackResponse]:
    """
    Record feedback for a run to LangSmith.

    This is a simple wrapper for the LangSmith create_feedback API, so the
    credentials can be stored and managed in the service rather than the client.
    See: https://api.smith.langchain.com/redoc#tag/feedback/operation/create_feedback_api_v1_feedback_post
    """
    identity = request.state.principal
    repo = repository(request)
    if not identity.admin:
        assert identity.user_id
        if repo is None or not await repo.owns_run(feedback.run_id, identity.user_id):
            raise HTTPException(403, "Run belongs to another user")

    client = LangsmithClient()
    kwargs = feedback.kwargs or {}
    client.create_feedback(
        run_id=feedback.run_id,
        key=feedback.key,
        score=feedback.score,
        **kwargs,
    )
    return api_success(FeedbackResponse())


@router.post("/{agent_id}/history", operation_id="history_with_agent_id")
@router.post("/history")
async def history(
    input: ChatHistoryInput, request: Request, agent_id: str = DEFAULT_AGENT
) -> ApiResponse[ChatHistory]:
    """
    Get chat history for a thread and agent.

    If agent_id is not provided, the default agent will be used.
    """
    agent: AgentGraph = get_agent(agent_id)
    await authorize_thread(
        repository(request), agent, input.thread_id, agent_id, request.state.principal
    )
    config = RunnableConfig(configurable={"thread_id": input.thread_id})
    try:
        messages: list[BaseMessage] = []
        # Functional-API agents keep the conversation in `__previous__`, which aget_state
        # doesn't return, so read the raw checkpoint first and only fall back for graphs.
        checkpointer = getattr(agent, "checkpointer", None)
        if checkpointer:
            tup = await checkpointer.aget_tuple(config)
            if tup and "__previous__" in (tup.checkpoint.get("channel_values") or {}):
                messages = messages_from_checkpoint(tup.checkpoint)
        if not messages:
            state_snapshot = await agent.aget_state(config=config)
            messages = state_snapshot.values.get("messages", [])
        chat_messages: list[ChatMessage] = [langchain_to_chat_message(m) for m in messages]
        return api_success(ChatHistory(messages=chat_messages))
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error")


@router.get("/{agent_id}/threads", operation_id="threads_with_agent_id")
@router.get("/threads")
async def threads(
    request: Request, input: UserThreadsInput = Depends(), agent_id: str = DEFAULT_AGENT
) -> ApiResponse[UserThreads]:
    """
    List a user's conversation threads for an agent, most recently updated first.

    App access tokens are bound to their authenticated user. Trusted service
    credentials may query an explicit user_id for administrative integrations.
    """
    input.user_id = bind_user(request.state.principal, input.user_id)
    agent: AgentGraph = get_agent(agent_id)
    checkpointer = getattr(agent, "checkpointer", None)
    if not checkpointer:
        return api_success(UserThreads(threads=[]))

    try:
        summaries = await list_user_threads(checkpointer, input.user_id, agent_id, input.limit)
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error")

    return api_success(UserThreads(threads=summaries))


@router.get("/{agent_id}/chat-records", operation_id="chat_records_with_agent_id")
@router.get("/chat-records")
async def chat_records(
    request: Request, input: ChatRecordsInput = Depends(), agent_id: str = DEFAULT_AGENT
) -> ApiResponse[ChatRecordList]:
    """List the latest 30 UTC calendar days containing chat activity."""
    input.user_id = bind_user(request.state.principal, input.user_id)
    agent: AgentGraph = get_agent(agent_id)
    checkpointer = getattr(agent, "checkpointer", None)
    if not checkpointer:
        return api_success(ChatRecordList(records=[]))

    try:
        records = await list_chat_record_days(checkpointer, input.user_id, agent_id)
    except Exception as e:
        logger.exception("An exception occurred while listing chat records")
        raise HTTPException(status_code=500, detail="Unexpected error") from e
    return api_success(ChatRecordList(records=records))


@router.get(
    "/{agent_id}/chat-records/{record_date}",
    operation_id="chat_record_detail_with_agent_id",
)
@router.get("/chat-records/{record_date}")
async def chat_record_detail(
    record_date: date,
    request: Request,
    input: ChatRecordsInput = Depends(),
    agent_id: str = DEFAULT_AGENT,
) -> ApiResponse[ChatRecordDetail]:
    """Return messages added to conversations on one UTC calendar day."""
    first_date, current_date = recent_record_dates(datetime.now(UTC))
    if not first_date <= record_date <= current_date:
        raise HTTPException(status_code=422, detail="record_date must be within the latest 30 days")

    input.user_id = bind_user(request.state.principal, input.user_id)
    agent: AgentGraph = get_agent(agent_id)
    checkpointer = getattr(agent, "checkpointer", None)
    if not checkpointer:
        return api_success(ChatRecordDetail(record_date=record_date, conversations=[]))

    try:
        conversations = await get_chat_record_detail(
            checkpointer, input.user_id, agent_id, record_date
        )
    except Exception as e:
        logger.exception("An exception occurred while retrieving a chat record")
        raise HTTPException(status_code=500, detail="Unexpected error") from e
    return api_success(ChatRecordDetail(record_date=record_date, conversations=conversations))
