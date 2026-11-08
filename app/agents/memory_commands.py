"""Voice-friendly memory management with scoped lookup and explicit confirmation."""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai.chat_models.base import BaseChatOpenAI
from pydantic import BaseModel

from app.core import settings
from app.schemas.execution import MemoryConfirmation
from app.schemas.memory import MemoryRecord
from app.schemas.memory_commands import (
    MemoryDialogue,
    MemoryIntent,
    MemoryMatch,
    MemoryMutation,
    MemoryTarget,
    PendingMemoryCommand,
)
from app.services.execution import confirmation_id, progress
from app.services.memory.mem0_service import Mem0Service, MemoryConflictError, MemoryNotFoundError

logger = logging.getLogger(__name__)


MEMORY_HINT = re.compile(
    r"记忆|记住|记着|记得|记录|偏好|喜好|个人信息|忘记|忘掉|忘了|"
    r"保存的|存储的|remember|memor|forget",
    re.IGNORECASE,
)


COMMAND_HINT = re.compile(
    MEMORY_HINT.pattern + r"|删|清空|移除|抹掉|修改|改成|改为|更正|更新|delete|update|erase",
    re.IGNORECASE,
)


CONFIRMATIONS = {
    "确认",
    "确定",
    "是",
    "是的",
    "对",
    "对的",
    "好",
    "好的",
    "可以",
    "执行",
    "确认修改",
    "确认删除",
    "确认清空",
    "确定修改",
    "确定删除",
    "确定清空",
    "yes",
    "confirm",
    "confirmed",
}


CANCELLATIONS = {
    "取消",
    "取消操作",
    "取消修改",
    "取消删除",
    "取消清空",
    "不要",
    "不要了",
    "不用了",
    "算了",
    "不",
    "不确认",
    "别删",
    "别删除",
    "不要删除",
    "不要修改",
    "不要清空",
    "先别删",
    "别改",
    "先别改",
    "no",
    "cancel",
    "stop",
}


async def _structured[Result: BaseModel](
    model: BaseChatModel,
    schema: type[Result],
    prompt: str,
    data: dict[str, object],
    config: RunnableConfig,
) -> Result:
    structured = (
        model.with_structured_output(schema, method="function_calling")
        if isinstance(model, BaseChatOpenAI)
        else model.with_structured_output(schema)
    )
    chain = structured.with_config(tags=["skip_stream"])
    async with asyncio.timeout(settings.MEM0_TIMEOUT_SECONDS):
        result = await chain.ainvoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=json.dumps(data, ensure_ascii=False)),
            ],
            config,
        )
    return schema.model_validate(result)


async def parse_memory_intent(
    model: BaseChatModel, text: str, dialogue: MemoryDialogue, config: RunnableConfig
) -> MemoryIntent:
    return await _structured(
        model,
        MemoryIntent,
        settings.mem0_prompts.intent,
        {
            "current_user_request": text,
            "previous_choices_data": [choice.memory for choice in dialogue.choices],
            "last_memory_data": dialogue.last_target.memory if dialogue.last_target else None,
        },
        config,
    )


async def match_memories(
    model: BaseChatModel, query: str, records: list[MemoryRecord], config: RunnableConfig
) -> list[MemoryTarget]:
    result = await _structured(
        model,
        MemoryMatch,
        settings.mem0_prompts.match,
        {
            "requested_old_fact": query,
            "candidate_memories_data": [record.memory for record in records],
        },
        config,
    )
    indices = list(dict.fromkeys(result.matching_indices))
    if any(index < 1 or index > len(records) for index in indices):
        raise ValueError("The model selected an unknown memory")
    return [_target(records[index - 1]) for index in indices]


def _target(record: MemoryRecord) -> MemoryTarget:
    return MemoryTarget(id=record.id, memory=record.memory, updated_at=record.updated_at)


def _normalized(text: str) -> str:
    normalized = re.sub(r"[\s，。！？、,.!?;；:：]", "", text).lower()
    spoken = re.sub(r"^(?:(?:嗯+|呃+)|好的?|是的|那就|请)+", "", normalized)
    spoken = re.sub(r"(?:吧|就行|即可)$", "", spoken)
    return spoken if spoken in CONFIRMATIONS | CANCELLATIONS else normalized


def _selection(text: str) -> int | None:
    match = re.fullmatch(
        r"(?:请)?(?:选|选择|我选|我选择|我要|就是|就选)?第?"
        r"([一二两三四五六七八九十]|\d+)(?:条|个|条记忆)?(?:吧|就行|即可)?",
        text,
    )
    if match is None:
        return None
    value = match[1]
    return (
        int(value)
        if value.isdecimal()
        else {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }[value]
    )


def _reply(text: str, status: str) -> AIMessage:
    return AIMessage(content=text, response_metadata={"memory_status": status})


def _confirmation(pending: PendingMemoryCommand) -> str:
    if pending.action == "delete_all" and pending.snapshot is not None:
        return (
            f"将删除你当前的全部 {pending.snapshot.count} 条长期记忆，保留聊天记录和变更历史。"
            "请说确认清空，或说取消。"
        )
    if pending.action == "delete_many":
        contents = "；".join(f"“{target.memory}”" for target in pending.choices)
        return f"将删除以下 {len(pending.choices)} 条长期记忆：{contents}。请说确认删除，或说取消。"
    if not pending.choices:
        raise ValueError("A selected memory is required")
    original = pending.choices[0].memory
    if pending.action == "update":
        return f"将把记忆“{original}”修改为“{pending.text}”。请说确认修改，或说取消。"
    return f"将删除记忆“{original}”。请说确认删除，或说取消。"


async def _history(
    memory: Mem0Service, user_id: str, target: MemoryTarget, dialogue: MemoryDialogue
) -> AIMessage:
    history = await memory.memory_history(str(target.id), user_id, limit=5)
    dialogue.last_target = target
    dialogue.pending = None
    if not history.history:
        return _reply("这条记忆没有可查询的变更历史。", "history")
    descriptions = []
    for entry in history.history:
        if entry.event == "ADD":
            descriptions.append(f"新增“{entry.new_memory}”")
        elif entry.event == "UPDATE":
            descriptions.append(f"从“{entry.old_memory}”修改为“{entry.new_memory}”")
        else:
            descriptions.append(f"删除“{entry.old_memory}”")
    suffix = "还有更多历史，可通过记忆历史接口分页查看。" if history.next_cursor else ""
    return _reply("这条记忆的变更依次是：" + "；".join(descriptions) + "。" + suffix, "history")


async def _confirm(memory: Mem0Service, user_id: str, dialogue: MemoryDialogue) -> AIMessage:
    pending = dialogue.pending
    if pending is None or pending.stage != "confirm" or pending.action == "history":
        return _reply("当前没有等待确认的记忆操作，请先说明要修改或删除哪条记忆。", "no_pending")
    target = pending.choices[0] if pending.choices else None
    mutation = MemoryMutation(
        action=pending.action,
        target=None if pending.action == "delete_many" else target,
        targets=pending.choices if pending.action == "delete_many" else [],
        text=pending.text,
        revision=pending.snapshot.revision if pending.snapshot else None,
    )
    result = await memory.apply_memory_mutation(user_id, mutation)
    dialogue.pending = None
    dialogue.last_target = target
    dialogue.choices = []
    if result.status == "cleared":
        return _reply(f"已清空你的长期记忆，本次删除 {result.deleted_count} 条。", "cleared")
    if result.status == "deleted":
        if pending.action == "delete_many":
            return _reply(f"已删除选中的 {result.deleted_count} 条长期记忆。", "deleted")
        return _reply(f"已删除记忆“{target.memory if target else ''}”。", "deleted")
    if target is not None:
        dialogue.last_target = target.model_copy(update={"memory": pending.text})
    return _reply(f"这条记忆已更新为“{pending.text}”。", result.status)


async def _resolve(
    intent: MemoryIntent,
    memory: Mem0Service,
    user_id: str,
    dialogue: MemoryDialogue,
    model: BaseChatModel,
    config: RunnableConfig,
) -> list[MemoryTarget]:
    if intent.selection is not None:
        if intent.selection > len(dialogue.choices):
            return []
        target = dialogue.choices[intent.selection - 1]
    elif not intent.query.strip():
        target = dialogue.last_target
    else:
        result = await memory.search_memory_records(intent.query, user_id)
        if not result.memories:
            return []
        return await match_memories(model, intent.query, result.memories, config)
    if target is None:
        return []
    if intent.action == "history":
        return [target]
    return [_target(await memory.get_memory(str(target.id), user_id))]


async def _handle(
    text: str,
    memory: Mem0Service,
    user_id: str,
    dialogue: MemoryDialogue,
    model: BaseChatModel,
    config: RunnableConfig,
) -> AIMessage | None:
    normalized = _normalized(text)
    pending = dialogue.pending
    if normalized in CANCELLATIONS and pending is not None:
        dialogue.pending = None
        return _reply("已取消这次待确认的记忆操作。", "cancelled")
    if pending is not None and pending.expires_at <= datetime.now(UTC):
        dialogue.pending = None
        return _reply("刚才待确认的记忆操作已过期，请重新说明。", "expired")
    if normalized in CONFIRMATIONS:
        if pending is not None and pending.stage == "select":
            return _reply("请先按编号选择一条候选记忆，再确认操作。", "selection_required")
        explicit_action = next(
            (
                action
                for word, action in [("修改", "update"), ("删除", "delete"), ("清空", "delete_all")]
                if word in normalized
            ),
            None,
        )
        expected_action = (
            "delete"
            if pending and pending.action == "delete_many"
            else (pending.action if pending else None)
        )
        if pending is not None and explicit_action and explicit_action != expected_action:
            return _reply(
                "你确认的操作与刚才的预览不同，本次没有执行。" + _confirmation(pending),
                "confirmation_required",
            )
        return await _confirm(memory, user_id, dialogue)
    if pending is not None and pending.stage == "select":
        index = _selection(normalized)
        if index is not None:
            if not 1 <= index <= len(pending.choices):
                return _reply(
                    "这个编号不在刚才的候选中，请重新选择或说取消。", "selection_required"
                )
            target = pending.choices[index - 1]
            if pending.action == "history":
                return await _history(memory, user_id, target, dialogue)
            pending.choices = [target]
            pending.stage = "confirm"
            dialogue.last_target = target
            return _reply(_confirmation(pending), "confirmation_required")
    if pending is not None and not COMMAND_HINT.search(text):
        if normalized in {"再说一遍", "继续", "哪条", "是哪条"}:
            if pending.stage == "confirm":
                return _reply(_confirmation(pending), "confirmation_required")
        dialogue.pending = None
        return None
    intent = await parse_memory_intent(model, text, dialogue, config)
    dialogue.pending = None
    if intent.other_user:
        return _reply("只能管理你自己账号下的长期记忆。", "forbidden")
    if intent.action == "none":
        return None
    if intent.action == "count":
        snapshot = await memory.memory_snapshot(user_id)
        return _reply(f"你目前共有 {snapshot.count} 条长期记忆。", "counted")
    if intent.action == "delete_many" and intent.selections:
        indices = list(dict.fromkeys(intent.selections))
        if any(index < 1 or index > len(dialogue.choices) for index in indices):
            return _reply(
                "编号不在刚才的候选中，请先查询记忆，再指定要删除的编号。", "selection_required"
            )
        targets = [
            _target(await memory.get_memory(str(dialogue.choices[index - 1].id), user_id))
            for index in indices
        ]
        dialogue.pending = PendingMemoryCommand(
            action="delete_many",
            stage="confirm",
            choices=targets,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        return _reply(_confirmation(dialogue.pending), "confirmation_required")
    if intent.action == "list" and intent.selection is not None:
        targets = await _resolve(intent, memory, user_id, dialogue, model, config)
        if not targets:
            return _reply("没有找到指定编号的长期记忆，请先让我列出记忆。", "not_found")
        dialogue.last_target = targets[0]
        return _reply(f"这条记忆是：{targets[0].memory}。", "listed")
    if intent.action in {"list", "delete_many"}:
        result = (
            await memory.search_memory_records(intent.query, user_id)
            if intent.query.strip()
            else await memory.list_memories(user_id, limit=5)
        )
        dialogue.choices = [_target(record) for record in result.memories[:5]]
        if not dialogue.choices:
            return _reply("没有找到相关的长期记忆。", "not_found")
        text = "；".join(
            f"第{index}条，{target.memory}"
            for index, target in enumerate(dialogue.choices, start=1)
        )
        suffix = "还有更多记忆，可通过记忆列表接口分页查看。" if result.next_cursor else ""
        instruction = (
            "。这里只列出最多五条候选，请明确要删除的编号，例如：删除第一条和第三条记忆。"
            if intent.action == "delete_many"
            else "。你可以按编号指定要修改或删除的记忆。"
        )
        return _reply("我记得：" + text + instruction + suffix, "listed")
    if intent.action == "delete_all":
        snapshot = await memory.memory_snapshot(user_id)
        if snapshot.count == 0:
            await memory.apply_memory_mutation(
                user_id, MemoryMutation(action="delete_all", revision=snapshot.revision)
            )
            return _reply("你目前没有可清空的长期记忆。", "cleared")
        dialogue.pending = PendingMemoryCommand(
            action="delete_all",
            stage="confirm",
            snapshot=snapshot,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        return _reply(_confirmation(dialogue.pending), "confirmation_required")
    if intent.action == "update" and intent.text is None:
        return _reply(
            "请同时说明原来的记忆和新内容，例如：把我喜欢茶的记忆改成我喜欢咖啡。",
            "clarification_required",
        )
    targets = await _resolve(intent, memory, user_id, dialogue, model, config)
    if not targets:
        return _reply(
            "没有找到能明确对应的记忆，请具体描述原来的内容，或先让我列出记忆。", "not_found"
        )
    if intent.action == "history" and len(targets) == 1:
        return await _history(memory, user_id, targets[0], dialogue)
    dialogue.choices = targets
    dialogue.pending = PendingMemoryCommand(
        action=intent.action,
        stage="select" if len(targets) > 1 else "confirm",
        choices=targets,
        text=intent.text if intent.action == "update" else None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    if len(targets) == 1:
        dialogue.last_target = targets[0]
        return _reply(_confirmation(dialogue.pending), "confirmation_required")
    choices = "；".join(
        f"第{index}条，{target.memory}" for index, target in enumerate(targets, start=1)
    )
    return _reply(
        "找到多条可能的记忆：" + choices + "。请说第几条，或说取消。", "selection_required"
    )


async def handle_memory_command(
    text: str,
    memory: Mem0Service | None,
    user_id: str,
    dialogue: MemoryDialogue,
    model: BaseChatModel,
    config: RunnableConfig,
) -> AIMessage | None:
    submitted = config.get("configurable", {}).get("memory_confirmation")
    if submitted is not None:
        confirmation = MemoryConfirmation.model_validate(submitted)
        pending = dialogue.pending
        if (
            pending is None
            or pending.stage != "confirm"
            or pending.expires_at <= datetime.now(UTC)
            or confirmation.confirmation_id != confirmation_id(pending)
        ):
            return _reply("该确认已失效或操作已改变，请重新查询并确认。", "conflict")
        text = "确认" if confirmation.approved else "取消"
    explicit_confirmation = _normalized(text) in {
        "确认",
        "确定",
        "confirm",
        "确认修改",
        "确认删除",
        "确认清空",
        "确定修改",
        "确定删除",
        "确定清空",
    }
    if not dialogue.pending and not COMMAND_HINT.search(text) and not explicit_confirmation:
        return None
    await progress("memory.command", "处理记忆操作")
    try:
        if memory is None:
            dialogue.pending = None
            if not explicit_confirmation:
                if not MEMORY_HINT.search(text):
                    return None
                intent = await parse_memory_intent(model, text, dialogue, config)
                if intent.action == "none":
                    return None
            return _reply("长期记忆服务没有启用，当前无法查询、修改或删除长期记忆。", "disabled")
        return await _handle(text, memory, user_id, dialogue, model, config)
    except MemoryConflictError:
        dialogue.pending = None
        return _reply("记忆在等待确认期间发生了变化，本次没有执行，请重新选择并确认。", "conflict")
    except MemoryNotFoundError:
        dialogue.pending = None
        return _reply(
            "这条记忆已经不存在，或不属于你的账号，本次没有执行新的修改或删除。", "not_found"
        )
    except Exception as exc:
        dialogue.pending = None
        logger.warning("Conversational memory operation failed error_type=%s", type(exc).__name__)
        if memory is None:
            return _reply("长期记忆服务没有启用，当前无法查询、修改或删除长期记忆。", "disabled")
        return _reply(
            "记忆操作未能确认完成，可能仍在处理中。请先查询记忆或变更历史，再决定是否重试。",
            "operation_failed",
        )
