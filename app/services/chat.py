import logging
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langfuse.langchain import CallbackHandler
from langgraph.types import Command
from langsmith import uuid7
from pydantic import ValidationError

from app.agents import AgentGraph
from app.core import settings
from app.core.exceptions import ServiceError
from app.repositories.voice import VoiceRepository
from app.schemas import ChatMessage, UserInput
from app.schemas.execution import MemoryConfirmation
from app.services.agent_runner import delivery_context
from app.services.location import device_location_scope, needs_device_location
from app.services.memory.local_context import DeviceMemory, device_memory_scope
from app.services.messages import ensure_model_available, langchain_to_chat_message

logger = logging.getLogger(__name__)


async def build_run_input(
    user_input: UserInput,
    agent: AgentGraph,
    agent_id: str,
    repo: VoiceRepository | None = None,
    *,
    auto_resume: bool = True,
    authenticated_user_id: str | None = None,
) -> tuple[dict[str, Any], UUID]:
    """
    Parse user input and handle any required interrupt resumption.
    Returns kwargs for agent invocation and the run_id.
    """
    run_id = uuid7()
    thread_id = user_input.thread_id or str(uuid4())
    user_id = user_input.user_id or str(uuid4())

    configurable: dict[str, Any] = {"thread_id": thread_id, "user_id": user_id}
    if user_input.model is not None:
        ensure_model_available(user_input.model)
        configurable["model"] = user_input.model

    callbacks: list[Any] = []
    if settings.LANGFUSE_TRACING:
        # Initialize Langfuse CallbackHandler for Langchain (tracing)
        langfuse_handler = CallbackHandler()

        callbacks.append(langfuse_handler)

    if user_input.agent_config:
        # Check for reserved keys (including 'model' even if not in configurable)
        reserved_keys = {
            "thread_id",
            "user_id",
            "model",
            "checkpoint_id",
            "checkpoint_ns",
            "authenticated_user_id",
            "execution_run_id",
            "execution_response_id",
            "local_memory",
            "device_location",
            "memory_mode",
        }
        if overlap := reserved_keys & user_input.agent_config.keys():
            raise ServiceError(
                status_code=422,
                detail=f"agent_config contains reserved keys: {overlap}",
            )
        if "memory_confirmation" in user_input.agent_config:
            try:
                MemoryConfirmation.model_validate(user_input.agent_config["memory_confirmation"])
            except ValidationError:
                raise ServiceError(422, "Invalid memory confirmation") from None
        configurable.update(user_input.agent_config)

    configurable["authenticated_user_id"] = authenticated_user_id

    config = RunnableConfig(
        configurable=configurable,
        metadata={"user_id": user_id, "agent_id": agent_id, "execution_run_id": str(run_id)},
        run_id=run_id,
        callbacks=callbacks,
    )

    # Check for interrupts that need to be resumed
    state = await agent.aget_state(config=config)

    interrupted_tasks = [
        task for task in state.tasks if hasattr(task, "interrupts") and task.interrupts
    ]

    input: Command | dict[str, Any]
    if interrupted_tasks and auto_resume:
        # assume user input is response to resume agent execution from interrupt
        input = Command(resume=user_input.message)
    else:
        context = await delivery_context(repo, agent, thread_id) if repo else []
        input = {"messages": context + [HumanMessage(content=user_input.message)]}

    kwargs = {
        "input": input,
        "config": config,
    }
    if user_input.local_memory is not None:
        if not settings.LOCAL_MEMORY_ENABLED:
            raise ServiceError(503, "Device memory is disabled")
        kwargs["_device_memory"] = DeviceMemory(user_id, thread_id, user_input.local_memory)
    if (
        user_input.device_location is not None
        and user_input.device_location.is_fresh()
        and needs_device_location(user_input.message)
    ):
        kwargs["_device_location"] = user_input.device_location

    if repo:
        await repo.register_run(str(run_id), user_id, thread_id)

    return kwargs, run_id


async def invoke_agent(
    user_input: UserInput,
    agent: AgentGraph,
    agent_id: str,
    repo: VoiceRepository | None,
    *,
    authenticated_user_id: str | None = None,
) -> ChatMessage:
    """
    Invoke an agent with user input to retrieve a final response.

    If agent_id is not provided, the default agent will be used.
    Use thread_id to persist and continue a multi-turn conversation. run_id kwarg
    is also attached to messages for recording feedback.
    Use user_id to persist and continue a conversation across multiple threads.
    """
    kwargs, run_id = await build_run_input(
        user_input, agent, agent_id, repo, authenticated_user_id=authenticated_user_id
    )

    try:
        with (
            device_memory_scope(kwargs.pop("_device_memory", None)),
            device_location_scope(kwargs.pop("_device_location", None)),
        ):
            response_events: list[tuple[str, Any]] = await agent.ainvoke(**kwargs, stream_mode=["updates", "values"])  # type: ignore # fmt: skip
        response_type, response = response_events[-1]
        # A run that stops on an interrupt reports it on the final event of either stream
        # mode, so check for the interrupt before falling back to the last message.
        if "__interrupt__" in response:
            # Return the value of the first interrupt as an AIMessage
            output = langchain_to_chat_message(
                AIMessage(content=response["__interrupt__"][0].value)
            )
        elif response_type == "values":
            # Normal response, the agent completed successfully
            output = langchain_to_chat_message(response["messages"][-1])
        else:
            raise ValueError(f"Unexpected response type: {response_type}")

        output.run_id = str(run_id)
        return output
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
        raise ServiceError(status_code=500, detail="Unexpected error")
