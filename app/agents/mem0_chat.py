"""A streaming chatbot with thread history and user-scoped long-term Mem0 facts."""

import json
import logging
from time import monotonic
from typing import Any, NotRequired, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_store, get_stream_writer
from langgraph.func import entrypoint
from pydantic import ValidationError

from app.agents.chat import answer_chat
from app.agents.email_commands import handle_email_command
from app.agents.memory_commands import handle_memory_command
from app.core import get_model, settings
from app.schemas.execution import ExecutionConfirmation
from app.schemas.memory_commands import MemoryDialogue
from app.services.execution import ExecutionTrace, confirmation_id, progress
from app.services.memory.mem0_service import Mem0Service, get_mem0_service
from app.services.skills.video_chat import VideoSkillSession

logger = logging.getLogger(__name__)


CHAT_MESSAGE_LIMIT = 20


class Mem0ChatState(TypedDict):
    messages: list[BaseMessage]
    memory_dialogue: NotRequired[dict[str, Any]]
    email_dialogue: NotRequired[dict[str, Any] | None]
    video_dialogue: NotRequired[dict[str, Any] | None]


def _chat_prompt(messages: list[BaseMessage], facts: list[str], status: str) -> list[BaseMessage]:
    prefix: list[BaseMessage] = [
        SystemMessage(content=settings.mem0_prompts.chat_system),
    ]
    recent_messages = [
        message for message in messages if isinstance(message, (HumanMessage, AIMessage))
    ][-CHAT_MESSAGE_LIMIT:]
    prefix.extend(recent_messages[:-1])
    prefix.append(
        SystemMessage(
            content=settings.mem0_prompts.memory_status_template.format(status=status),
            name="runtime_context",
        )
    )
    if facts:
        prefix.append(
            HumanMessage(
                content=json.dumps({"untrusted_user_memories": facts}, ensure_ascii=False),
                name="retrieved_memories",
            )
        )
    return prefix + recent_messages[-1:]


def _extraction_context(messages: list[BaseMessage]) -> list[dict[str, str]]:
    context: list[dict[str, str]] = []
    remaining = settings.MEM0_MAX_CONTEXT_CHARS
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.response_metadata.get("memory_status") in {
            "updated",
            "unchanged",
            "deleted",
            "cleared",
        }:
            break
        if not isinstance(message, (HumanMessage, AIMessage)) or not isinstance(
            message.content, str
        ):
            continue
        if isinstance(message, AIMessage) and message.response_metadata.get("research_status"):
            continue
        if len(message.content) > remaining:
            break
        context.append(
            {
                "role": "user" if isinstance(message, HumanMessage) else "assistant",
                "content": message.content,
            }
        )
        remaining -= len(message.content)
        if len(context) == 10:
            break
    return list(reversed(context))


async def _retrieve_facts(
    memory: Mem0Service | None, user_messages: list[dict[str, str]], user_id: str
) -> tuple[list[str], str]:
    if memory is None:
        return [], "disabled"
    if not user_messages:
        return [], "available"
    try:
        return await memory.search(user_messages[-1]["content"], user_id), "available"
    except Exception as exc:
        logger.warning("Mem0 search failed error_type=%s", type(exc).__name__)
        return [], "search_failed"


async def _mem0_chat(
    inputs: dict[str, list[BaseMessage]],
    *,
    previous: Mem0ChatState | None,
    config: RunnableConfig,
) -> entrypoint.final:
    started = monotonic()
    configurable = config.get("configurable", {})
    user_id = configurable.get("user_id")
    thread_id = configurable.get("thread_id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("A bound user_id is required for mem0-chatbot")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id is required for mem0-chatbot")
    new_messages = inputs["messages"]
    user_messages = [
        {"role": "user", "content": message.content}
        for message in new_messages
        if isinstance(message, HumanMessage)
        and isinstance(message.content, str)
        and message.content.strip()
    ]
    messages = (previous["messages"] if previous else []) + new_messages
    video_dialogue = previous.get("video_dialogue") if previous else None
    memory = get_mem0_service()
    dialogue = MemoryDialogue(user_id=user_id)
    saved_dialogue = previous.get("memory_dialogue") if previous else None
    if saved_dialogue:
        try:
            restored = MemoryDialogue.model_validate(saved_dialogue)
            if restored.user_id == user_id:
                dialogue = restored
        except ValidationError:
            logger.warning("Discarded invalid memory dialogue checkpoint")
    model_name = configurable.get("model", settings.DEFAULT_MODEL)
    thinking = configurable.get(
        "voice_enable_thinking",
        configurable.get("enable_thinking", settings.CHAT_LLM_ENABLE_THINKING),
    )
    model = get_model(
        model_name,
        enable_thinking=thinking
        if isinstance(thinking, bool)
        else settings.CHAT_LLM_ENABLE_THINKING,
    )
    if user_messages:
        email_response, email_dialogue = await handle_email_command(
            next(
                message for message in reversed(new_messages) if isinstance(message, HumanMessage)
            ),
            previous.get("email_dialogue") if previous else None,
            model,
            config,
        )
        if email_response is not None:
            return entrypoint.final(
                value={"messages": [email_response]},
                save={
                    "messages": messages + [email_response],
                    "memory_dialogue": dialogue.model_dump(mode="json"),
                    "email_dialogue": email_dialogue,
                    "video_dialogue": video_dialogue,
                },
            )
        command_response = await handle_memory_command(
            user_messages[-1]["content"], memory, user_id, dialogue, model, config
        )
        if command_response is not None:
            logger.info(
                "Memory command completed memory_status=%s duration_ms=%d",
                command_response.response_metadata["memory_status"],
                int((monotonic() - started) * 1000),
            )
            return entrypoint.final(
                value={"messages": [command_response]},
                save={
                    "messages": messages + [command_response],
                    "memory_dialogue": dialogue.model_dump(mode="json"),
                    "video_dialogue": video_dialogue,
                },
            )
    logger.info("Mem0 chatbot started memory_enabled=%s", memory is not None)
    await progress("memory.search", "检索相关记忆")
    facts, status = await _retrieve_facts(memory, user_messages, user_id)
    await progress(
        "memory.search",
        "检索相关记忆",
        "warning" if status == "search_failed" else "succeeded",
        "记忆检索暂不可用，继续回答"
        if status == "search_failed"
        else (
            "记忆服务未启用"
            if status == "disabled"
            else ("已找到相关记忆" if facts else "未找到相关记忆")
        ),
    )
    prompt = _chat_prompt(messages, facts, status)
    logger.info("Mem0 answer started preparation_ms=%d", int((monotonic() - started) * 1000))
    human = next(
        (message for message in reversed(new_messages) if isinstance(message, HumanMessage)),
        None,
    )
    video = (
        VideoSkillSession(human, video_dialogue, messages[:-1], get_stream_writer())
        if human is not None
        else None
    )
    await progress("chat.answer", "生成回复")
    response = await answer_chat(model, prompt, config, video=video)
    await progress("chat.answer", "生成回复", "succeeded")
    if video is not None:
        video_dialogue = video.saved
    if response.response_metadata.get("video_status"):
        return entrypoint.final(
            value={"messages": [response]},
            save={
                "messages": messages + [response],
                "memory_dialogue": dialogue.model_dump(mode="json"),
                "video_dialogue": video_dialogue,
            },
        )
    if memory and user_messages:
        await progress("memory.write", "整理本轮记忆")
        try:
            await memory.remember(
                [user_messages[-1]], user_id, thread_id, context=_extraction_context(messages[:-1])
            )
            status = "saved" if status == "available" else "saved_search_failed"
        except Exception as exc:
            status = "write_failed"
            logger.warning("Mem0 write failed error_type=%s", type(exc).__name__)
    if memory and user_messages:
        await progress(
            "memory.write",
            "整理本轮记忆",
            "warning" if status == "write_failed" else "succeeded",
            "记忆写入未确认，请稍后查询" if status == "write_failed" else "记忆处理完成",
        )
    response.response_metadata["memory_status"] = status
    logger.info(
        "Mem0 chatbot completed memory_status=%s duration_ms=%d",
        status,
        int((monotonic() - started) * 1000),
    )
    return entrypoint.final(
        value={"messages": [response]},
        save={
            "messages": messages + [response],
            "memory_dialogue": dialogue.model_dump(mode="json"),
            "video_dialogue": video_dialogue,
        },
    )


@entrypoint()
async def mem0_chat(
    inputs: dict[str, list[BaseMessage]],
    *,
    previous: Mem0ChatState | None,
    config: RunnableConfig,
) -> entrypoint.final:
    values = config.get("configurable", {})
    for name in ("user_id", "thread_id"):
        if not isinstance(values.get(name), str) or not values[name].strip():
            raise ValueError(f"A bound {name} is required for mem0-chatbot")
    latest = inputs["messages"][-1] if inputs["messages"] else None
    trace = ExecutionTrace(config, get_store(), get_stream_writer(), latest.id if latest else None)
    async with trace.bind():
        result = await _mem0_chat(inputs, previous=previous, config=config)
        response = result.value["messages"][-1]
        saved = result.save.get("memory_dialogue")
        dialogue = MemoryDialogue.model_validate(saved) if saved else None
        if (
            dialogue
            and dialogue.pending
            and dialogue.pending.stage == "confirm"
            and response.response_metadata.get("memory_status") == "confirmation_required"
        ):
            trace.snapshot.confirmation = ExecutionConfirmation(
                confirmation_id=confirmation_id(dialogue.pending),
                label=str(response.content),
                expires_at=dialogue.pending.expires_at,
            )
        await trace.finish(response)
        return result
