"""Execute the Nacos video skill through authenticated, idempotent video services."""

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter
from pydantic import ValidationError

from app.core import settings
from app.schemas.events import CustomData
from app.schemas.video import (
    VideoCreate,
    VideoListQuery,
    VideoStatus,
    VideoStorageStatus,
    VideoTask,
)
from app.schemas.video_skill import VideoSkillDialogue, VideoSkillIntent
from app.services.execution import record_video_result
from app.services.video.runtime import get_video_service
from app.services.video.service import (
    VideoBusyError,
    VideoConflictError,
    VideoEditInputError,
    VideoService,
)

logger = logging.getLogger(__name__)


class VideoSkillInputError(ValueError):
    pass


_UNSUPPORTED = {
    "duration": "HappyHorse 支持 3 至 15 秒整数；万相 3.0 支持 2 至 30 秒整数或 -1（智能时长）。请调整时长或明确选择模型。",
    "source": "目前只能编辑你已生成并转存的视频，请提供本地视频编号；暂不支持外部视频或影视片段截取。",
    "editing": "目前支持风格变换和局部替换，不能精确裁剪、拼接、加字幕或改变原视频时长和比例。",
    "voice_clone": "可以描述声音和台词，但尚未接入指定人物的声音克隆，也不能保证完全复刻原声。",
    "playback": "视频链接可以返回客户端展示和播放，当前语音技能不能直接控制播放器。",
    "selection": "请提供整数视频编号，或按标题、内容关键词查询列表后再说第几条；暂不支持按日期筛选。",
    "parameters": "请求参数超出支持范围。HappyHorse 为 3 至 15 秒；万相 3.0 为 2 至 30 秒或智能时长，支持首帧及首尾帧 HTTP(S) 图片；编辑支持 720P 或 1080P。请调整要求。",
    "other": "当前支持文生视频、万相 3.0 首帧及首尾帧生成、详情、分页列表、数量统计和已生成视频的风格编辑。该操作暂未接入。",
}


def _reply(text: str, status: str) -> AIMessage:
    return AIMessage(content=text, response_metadata={"video_status": status})


def _restore(saved: dict[str, Any] | None, user_id: str) -> VideoSkillDialogue:
    if saved:
        try:
            dialogue = VideoSkillDialogue.model_validate(saved)
            if dialogue.user_id == user_id:
                if dialogue.draft_expires_at <= datetime.now(UTC).timestamp():
                    _clear_draft(dialogue)
                return dialogue
        except ValidationError:
            logger.warning("视频技能上下文无效，已忽略")
    return VideoSkillDialogue(user_id=user_id)


def _clear_draft(dialogue: VideoSkillDialogue) -> None:
    dialogue.draft_request = None
    dialogue.draft_action = None
    dialogue.draft_generation = None
    dialogue.draft_editing = None
    dialogue.draft_expires_at = 0


def _history(messages: list[BaseMessage]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    budget = 12000
    for message in reversed(messages[-12:]):
        if not isinstance(message, (HumanMessage, AIMessage)) or not isinstance(
            message.content, str
        ):
            continue
        content = message.content[-min(3000, budget) :]
        if not content:
            break
        result.append({"role": message.type, "content": content})
        budget -= len(content)
        if budget <= 0:
            break
    return list(reversed(result))


def _task_data(task: VideoTask) -> dict[str, object]:
    return {
        "record_id": task.id,
        "model": task.model,
        "first_frame_url": task.first_frame_url,
        "last_frame_url": task.last_frame_url,
        "title": task.title,
        "cover_url": task.cover_url,
        "status": task.status.value,
        "storage_status": task.storage_status.value,
        "qiniu_video_url": task.qiniu_video_url,
        "error_code": task.error_code,
    }


def _task_text(task: VideoTask) -> str:
    if task.error_code == "QUERY_FAILED":
        return "本次刷新失败，显示的是已保存状态；任务已保留，请稍后查询。"
    if task.status == VideoStatus.SUCCEEDED:
        if task.error_code == "COVER_ARCHIVE_FAILED":
            return "视频已生成并保存，但封面提取或转存失败，后台会继续重试，无需重新生成视频。"
        if task.storage_status == VideoStorageStatus.STORED and not (task.title and task.cover_url):
            return "视频已生成并保存，标题或封面尚待补齐，请查询详情刷新。"
        return {
            VideoStorageStatus.STORED: "视频和封面已生成并转存，标题及链接已返回客户端。",
            VideoStorageStatus.FAILED: "已生成，但转存失败，后台会继续重试。",
        }.get(task.storage_status, "已生成，正在等待转存。")
    return {
        VideoStatus.SUBMITTING: "正在提交，请稍后查询。",
        VideoStatus.SUBMISSION_UNKNOWN: "提交结果暂不确定，请先核实原任务，避免重复生成。",
        VideoStatus.PENDING: "已提交，正在排队，请稍后查询。",
        VideoStatus.RUNNING: "正在生成，请稍后查询。",
        VideoStatus.FAILED: "生成失败，服务尚未返回具体原因。请核对参数或联系管理员查看任务记录；未自动重新提交。",
        VideoStatus.CANCELED: "上游任务已取消。",
        VideoStatus.UNKNOWN: "上游任务不存在或已过期，暂时无法获取结果。",
    }[task.status]


def _publish(
    response: AIMessage, kind: str, payload: dict[str, Any], writer: StreamWriter | None
) -> AIMessage:
    response.response_metadata["video"] = payload["video"] if kind == "video.task" else payload
    record_video_result(response.response_metadata["video"])
    if writer is not None:
        CustomData(data={"type": kind, **payload}).dispatch(writer)
    return response


def _save_draft(dialogue: VideoSkillDialogue, text: str, intent: VideoSkillIntent) -> None:
    if intent.reset_draft or (
        intent.action in {"create", "edit"}
        and dialogue.draft_action is not None
        and dialogue.draft_action != intent.action
    ):
        _clear_draft(dialogue)
    dialogue.draft_request = "\n".join(value for value in (dialogue.draft_request, text) if value)[
        -12000:
    ]
    dialogue.draft_expires_at = datetime.now(UTC).timestamp() + 600
    if intent.action in {"create", "edit"}:
        dialogue.draft_action = intent.action
    elif intent.editing is not None:
        dialogue.draft_action = "edit"
    elif intent.generation is not None:
        dialogue.draft_action = "create"
    if intent.generation is not None:
        previous = dialogue.draft_generation
        values = previous.model_dump(exclude_none=True) if previous else {}
        values.update(intent.generation.model_dump(exclude_none=True))
        dialogue.draft_generation = type(intent.generation).model_validate(values)
    if intent.editing is not None:
        previous_edit = dialogue.draft_editing
        values = previous_edit.model_dump(exclude_none=True) if previous_edit else {}
        values.update(intent.editing.model_dump(exclude_none=True))
        dialogue.draft_editing = type(intent.editing).model_validate(values)
    if intent.record_id is not None:
        dialogue.last_record_id = intent.record_id


async def _record_id(
    intent: VideoSkillIntent, dialogue: VideoSkillDialogue, service: VideoService, user_id: int
) -> int | None:
    if intent.record_id is not None:
        return intent.record_id
    if intent.selection is not None:
        if intent.selection > len(dialogue.listed_ids):
            raise VideoSkillInputError("这个序号不在刚才的视频列表中，请先查询列表或提供视频编号")
        return dialogue.listed_ids[intent.selection - 1]
    if intent.latest:
        return await service.repo.latest_id(user_id)
    return dialogue.last_record_id


async def _execute(
    intent: VideoSkillIntent,
    dialogue: VideoSkillDialogue,
    service: VideoService,
    user_id: int,
    key: str,
    writer: StreamWriter | None,
) -> AIMessage:
    search = (
        intent.action in {"detail", "edit"}
        and intent.query is not None
        and intent.query.q is not None
        and intent.record_id is None
        and intent.selection is None
    )
    if intent.action in {"list", "count"} or search:
        query = intent.query or VideoListQuery(page_size=5)
        if intent.next_page:
            if dialogue.last_query is None:
                return _reply("请先查询视频列表，再翻到下一页。", "missing")
            query = VideoListQuery.model_validate(
                {**dialogue.last_query.model_dump(), "page": dialogue.last_query.page + 1}
            )
        if intent.action == "count":
            query = query.model_copy(update={"page": 1, "page_size": 1})
        elif intent.latest and not intent.next_page:
            query = query.model_copy(update={"page": 1, "page_size": 1})
        page = await service.list_tasks(user_id, query)
        if intent.action == "count":
            return _publish(
                _reply(f"按当前条件查询，你共有 {page.total} 个视频任务。", "count"),
                "video.count",
                {"total": page.total, "query": query.model_dump(mode="json")},
                writer,
            )
        dialogue.last_query = query
        dialogue.listed_ids = [task.id for task in page.items]
        lines = [f"共 {page.total} 个视频任务，当前第 {page.page} 页，共 {page.total_pages} 页。"]
        lines.extend(
            f"第 {index} 条，{task.title[:80] + '，' if task.title else ''}"
            f"视频编号 {task.id}，{_task_text(task)}"
            for index, task in enumerate(page.items[:5], 1)
        )
        if not page.items:
            lines.append("当前页没有视频记录。")
        elif len(page.items) > 5:
            lines.append("语音先播报前五条，本页完整列表已返回客户端。")
        if page.items:
            lines.append("可按返回的标题和链接查看视频，或说查询第几条。")
            if page.page < page.total_pages:
                lines.append("还有更多结果，可以说下一页。")
            if intent.action == "edit":
                lines.append("请先选择要编辑的视频，本次尚未提交编辑。")
        return _publish(
            _reply("".join(lines), "list"),
            "video.list",
            {
                "items": [_task_data(task) for task in page.items],
                "total": page.total,
                "page": page.page,
                "page_size": page.page_size,
                "total_pages": page.total_pages,
                "query": query.model_dump(mode="json"),
            },
            writer,
        )
    if intent.action == "create":
        draft = dialogue.draft_generation
        if (
            draft is not None
            and draft.last_frame_url
            and not draft.first_frame_url
            and not draft.prompt
        ):
            return _reply(
                "首尾帧生成需要同时提供首帧图片，请补充可访问的 HTTP(S) 首帧地址；尾帧要求已保留。",
                "draft",
            )
        if draft is None or not ((draft.prompt and draft.prompt.strip()) or draft.first_frame_url):
            return _reply("请补充要生成的视频场景和内容；已有时长等要求会保留。", "draft")
        parameters = {**draft.model_dump(exclude_none=True), "watermark": False}
        if draft.model is None and (draft.first_frame_url or draft.last_frame_url):
            parameters["model"] = "wan3.0-video"
        body = VideoCreate.model_validate(parameters)
        task = await service.create(user_id, key, body)
    else:
        record_id = await _record_id(intent, dialogue, service, user_id)
        if record_id is None:
            return _reply("请提供视频编号、选择刚才列表的第几条，或说明查询最近的视频。", "missing")
        if intent.action == "edit":
            dialogue.last_record_id = record_id
            draft_edit = dialogue.draft_editing
            if draft_edit is None or not draft_edit.prompt or not draft_edit.prompt.strip():
                return _reply("请说明如何编辑视频，例如改成水墨画风格。", "draft")
            task = await service.edit(
                user_id, key, record_id, **draft_edit.model_dump(exclude_none=True)
            )
        else:
            task = await service.query(record_id, user_id)
    dialogue.last_record_id = task.id
    _clear_draft(dialogue)
    return _publish(
        _reply(f"视频编号 {task.id}，{_task_text(task)}", "result"),
        "video.task",
        {"video": _task_data(task)},
        writer,
    )


def _validation_message(exc: ValidationError) -> str:
    for error in exc.errors():
        cause = error.get("ctx", {}).get("error")
        if not error["loc"] and isinstance(cause, ValueError):
            return f"视频参数无效：{cause}。请调整要求，本次没有提交任务。"
    fields = {str(error["loc"][-1]) for error in exc.errors() if error["loc"]}
    if "duration" in fields:
        return _UNSUPPORTED["duration"]
    if "prompt" in fields:
        return "视频描述超出范围：HappyHorse 最多 2500 个中文字符或 5000 字符，万相 3.0 最多 20000 字符。请缩短描述。"
    if "title" in fields:
        return "视频标题须为 1 至 200 个字符，不能只包含空白。请调整标题。"
    if "cover_url" in fields:
        return "视频封面须为有效的 HTTP 或 HTTPS URL，且不超过 2048 个字符。请调整封面地址。"
    if "q" in fields:
        return "视频查询关键词须为 1 至 200 个字符，不能只包含空白。请调整关键词。"
    if {"page", "page_size"} & fields:
        return "页码须为正整数，每页支持 1 至 100 条，请调整分页参数。"
    if {"record_id", "selection"} & fields:
        return _UNSUPPORTED["selection"]
    if "seed" in fields:
        return "随机种子必须是 0 至 2147483647 的整数，万相 3.0 也支持 -1。请调整 seed。"
    if {"status", "storage_status"} & fields:
        return "视频状态无效，请说明要查询已生成、生成中、排队、失败或已转存的视频。"
    return _UNSUPPORTED["parameters"]


def video_skill_dialogue(
    saved: dict[str, Any] | None, config: RunnableConfig
) -> VideoSkillDialogue:
    values = config.get("configurable", {})
    user_id = values.get("authenticated_user_id")
    return (
        _restore(saved, user_id)
        if (
            isinstance(user_id, str)
            and user_id == values.get("user_id")
            and user_id.isascii()
            and user_id.isdecimal()
            and 0 < int(user_id) < 2**63
        )
        else VideoSkillDialogue(user_id="")
    )


def video_skill_reference(
    saved: dict[str, Any] | None,
    config: RunnableConfig,
    history: list[BaseMessage],
) -> str:
    dialogue = video_skill_dialogue(saved, config)
    skill = settings.video_skill
    if skill is None:
        raise ValueError("Video skill is unavailable")
    return json.dumps(
        {
            "skill_name": skill.name,
            "skill_reference": skill.instructions,
            "recent_history": _history(history),
            "dialogue": dialogue.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )


async def execute_video_skill(
    message: HumanMessage,
    saved: dict[str, Any] | None,
    intent: VideoSkillIntent,
    config: RunnableConfig,
    *,
    writer: StreamWriter | None = None,
) -> tuple[AIMessage | None, dict[str, Any] | None]:
    if not settings.VIDEO_SKILL_ENABLED or not isinstance(message.content, str):
        return None, saved
    skill = settings.video_skill
    if skill is None:
        return _reply("视频技能尚未从管理中心加载，请稍后再试。", "unavailable"), saved
    values = config.get("configurable", {})
    dialogue = video_skill_dialogue(saved, config)
    user_id = dialogue.user_id
    logger.info("视频技能执行：操作=%s 技能摘要=%s", intent.action, skill.digest[:12])
    if intent.action == "none":
        return None, saved
    if not user_id:
        return _reply("请先登录，再生成或查询你的视频。", "unauthenticated"), None
    service = get_video_service()
    if not settings.VIDEO_ENABLED or service is None:
        return _reply("视频服务暂未启用，请稍后再试。", "unavailable"), dialogue.model_dump(
            mode="json"
        )
    if intent.action == "cancel":
        text = (
            "已取消尚未提交的视频草稿。"
            if dialogue.draft_request
            else "没有待提交的草稿；已提交的视频任务暂不支持撤回。"
        )
        _clear_draft(dialogue)
        return _reply(text, "cancelled"), dialogue.model_dump(mode="json")
    if intent.action in {"create", "edit", "draft", "unsupported"}:
        if intent.authorized:
            _save_draft(dialogue, message.content, intent)
        elif intent.action != "unsupported":
            return _reply(
                "请明确说明要生成或编辑的视频，本次没有提交任务。", "clarification"
            ), saved
    if intent.action == "unsupported":
        response = _reply(_UNSUPPORTED[intent.reason or "other"], "unsupported")
    elif intent.action == "draft":
        response = _reply("请补充视频场景或编辑要求，已有描述和参数会保留。", "draft")
    else:
        request_id = message.id or str(config.get("run_id") or uuid4())
        digest = hashlib.sha256(
            json.dumps([user_id, values.get("thread_id"), request_id]).encode()
        ).hexdigest()
        try:
            response = await _execute(
                intent, dialogue, service, int(str(user_id)), "voice-video-" + digest, writer
            )
        except ValidationError as exc:
            response = _reply(_validation_message(exc), "invalid")
        except VideoConflictError:
            response = _reply(
                "此条输入已经提交过不同参数，请先查询原任务，避免重复生成。", "conflict"
            )
        except VideoBusyError:
            response = _reply("任务正在处理中，请稍后查询原任务，避免重复提交。", "busy")
        except LookupError:
            response = _reply("未找到你的视频，请核对本地整数编号或重新查询列表。", "missing")
        except (VideoEditInputError, VideoSkillInputError) as exc:
            response = _reply(str(exc), "invalid")
        except Exception as exc:
            logger.warning(
                "视频技能执行失败：操作=%s 异常类型=%s", intent.action, type(exc).__name__
            )
            response = _reply(
                "视频服务暂不可用；若刚才请求生成或编辑，请先查询最近任务，避免重复提交。",
                "unavailable",
            )
    logger.info(
        "视频技能出参：操作=%s 状态=%s", intent.action, response.response_metadata["video_status"]
    )
    return response, dialogue.model_dump(mode="json")
