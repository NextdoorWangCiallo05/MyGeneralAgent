"""对话接口：SSE 流式对话与停止生成。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, Store, rate_limited
from app.config import Settings
from app.core.agent import Agent
from app.core.confirmations import get_confirmations
from app.core.errors import (
    ApiKeyNotConfiguredError,
    AttachmentNotFoundError,
    ConfirmationNotFoundError,
    SessionNotFoundError,
    TooManyAttachmentsError,
)
from app.core.events import Event
from app.core.runtime_config import get_runtime_config
from app.core.tracing import flush_tracer
from app.llm import LLMNotConfiguredError, build_llm_client, resolve_llm_config
from app.schemas.auth import MessageResponse
from app.schemas.chat import ChatRequest, ConfirmRequest, StopRequest
from app.store.entities import Attachment, Session, User
from app.tools.registry import ToolRegistry, build_default_registry

logger = logging.getLogger("app.chat")
router = APIRouter(tags=["chat"])

# 单实例内的运行中任务，用于「停止生成」（多实例部署需改用 Redis，M5）
_running_tasks: dict[str, asyncio.Task] = {}
_registry: ToolRegistry = build_default_registry()

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 关闭 Nginx 缓冲，保证逐字下发
}


async def _resolve_session(
    payload: ChatRequest,
    user: User,
    store: Store,
    default_model: str,
) -> Session:
    if payload.session_id:
        session = await store.get_session(payload.session_id)
        if session is None or session.user_id != user.id:
            raise SessionNotFoundError()
        changes: dict[str, object] = {}
        if payload.model:
            changes["model"] = payload.model
        if payload.sandbox_enabled is not None:
            changes["sandbox_enabled"] = payload.sandbox_enabled
        if changes:
            await store.update_session(session, **changes)
        return session

    return await store.create_session(
        user_id=user.id,
        model=payload.model or default_model,
        sandbox_enabled=bool(payload.sandbox_enabled),
    )


async def _resolve_attachments(
    payload: ChatRequest,
    user: User,
    store: Store,
    settings: Settings,
) -> list[Attachment]:
    """取出发送时携带的附件：去重后校验数量与归属。

    放在会话创建之前校验，避免「附件有问题」却已经建出一个空会话。
    """
    attachment_ids = list(dict.fromkeys(payload.attachment_ids))
    if not attachment_ids:
        return []

    if len(attachment_ids) > settings.attachment_max_per_message:
        raise TooManyAttachmentsError(
            f"单条消息最多携带 {settings.attachment_max_per_message} 个附件"
        )

    items = await store.list_attachments(attachment_ids)
    if len(items) != len(attachment_ids) or any(item.user_id != user.id for item in items):
        raise AttachmentNotFoundError("部分附件不存在或已被清理，请重新上传后再发送")
    return items


@router.post("/chat", dependencies=[Depends(rate_limited("chat"))])
async def chat(payload: ChatRequest, user: CurrentUser, store: Store) -> StreamingResponse:
    """发送消息，以 SSE 流式返回 start/thinking/thinking_end/tool_call/tool_result/text/usage/done。"""
    # 业务配置以管理后台为准、环境变量兜底（见 app.core.runtime_config）
    settings = await get_runtime_config().effective_settings()
    # 厂商、服务地址、模型名由配置解析成一份确定的接入参数（见 app.llm.providers）
    try:
        llm = resolve_llm_config(settings)
    except LLMNotConfiguredError as exc:
        raise ApiKeyNotConfiguredError(str(exc)) from exc
    if llm.requires_api_key and not llm.api_key:
        raise ApiKeyNotConfiguredError(f"管理员尚未配置 {llm.label} 的 API-Key")

    attachments = await _resolve_attachments(payload, user, store, settings)
    session = await _resolve_session(payload, user, store, llm.model)
    logger.info(
        "开始生成",
        extra={
            "session_id": session.id,
            "user_id": user.id,
            "model": session.model,
            "provider": llm.provider,
            "attachments": len(attachments),
        },
    )
    client = build_llm_client(llm)
    agent = Agent(client, _registry, store, settings=settings)

    async def event_stream() -> AsyncIterator[str]:
        task = asyncio.current_task()
        if task is not None:
            _running_tasks[session.id] = task
        try:
            async for event in agent.run(
                user=user,
                session=session,
                user_input=payload.message,
                regenerate=payload.regenerate,
                attachments=attachments,
            ):
                yield event.to_sse()
        except asyncio.CancelledError:
            logger.info("会话 %s 的生成已被停止", session.id, extra={"session_id": session.id})
            raise
        except Exception:  # noqa: BLE001 - 兜底，避免流中断导致前端无提示
            logger.exception("会话 %s 流式响应异常", session.id, extra={"session_id": session.id})
            yield Event.error("INTERNAL_ERROR", "服务内部错误，请稍后重试").to_sse()
            yield Event.done("error").to_sse()
        finally:
            if _running_tasks.get(session.id) is task:
                _running_tasks.pop(session.id, None)
            # 链路埋点在流结束时统一上报（生成器可能被提前中断，这里保证一定 flush）
            flush_tracer()

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/chat/stop", response_model=MessageResponse)
async def stop(payload: StopRequest, user: CurrentUser, store: Store) -> MessageResponse:
    """停止指定会话正在进行的生成。"""
    session = await store.get_session(payload.session_id)
    if session is None or session.user_id != user.id:
        raise SessionNotFoundError()

    task = _running_tasks.get(payload.session_id)
    if task is not None and not task.done():
        task.cancel()
        return MessageResponse(message="已停止生成")
    return MessageResponse(message="当前没有正在进行的生成")


@router.post("/chat/confirm", response_model=MessageResponse)
async def confirm(payload: ConfirmRequest, user: CurrentUser, store: Store) -> MessageResponse:
    """确认或拒绝一个等待中的副作用工具调用（对应 SSE 的 `tool_confirm` 事件）。

    该接口在 `/chat` 的 SSE 流挂起期间调用；成功返回后原流会继续执行工具。
    请求不存在（已超时、已处理或会话不匹配）时返回 409。
    """
    session = await store.get_session(payload.session_id)
    if session is None or session.user_id != user.id:
        raise SessionNotFoundError()

    if not get_confirmations().resolve(payload.tool_call_id, payload.approved):
        logger.info("会话 %s 的确认请求已失效: %s", session.id, payload.tool_call_id)
        raise ConfirmationNotFoundError()

    logger.info(
        "会话 %s 的工具调用 %s 已%s", session.id, payload.tool_call_id, "允许" if payload.approved else "拒绝"
    )
    return MessageResponse(message="已允许执行" if payload.approved else "已拒绝执行")
