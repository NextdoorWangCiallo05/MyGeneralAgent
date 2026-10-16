"""会话接口：列表 / 新建 / 详情 / 消息历史 / 更新 / 删除。

存储后端由 `STORE_BACKEND` 决定（memory / sql），接口契约保持一致。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, Store
from app.core.errors import SessionNotFoundError
from app.schemas.attachment import AttachmentPublic
from app.schemas.auth import MessageResponse
from app.schemas.chat import (
    MessagePublic,
    SessionCreateRequest,
    SessionPublic,
    SessionUpdateRequest,
)
from app.store.entities import Session, User

router = APIRouter(prefix="/sessions", tags=["sessions"])


async def _get_owned(session_id: str, user: User, store: Store) -> Session:
    session = await store.get_session(session_id)
    if session is None or session.user_id != user.id:
        raise SessionNotFoundError()
    return session


@router.get("", response_model=list[SessionPublic])
async def list_sessions(user: CurrentUser, store: Store) -> list[SessionPublic]:
    sessions = await store.list_sessions(user.id)
    return [SessionPublic.model_validate(s) for s in sessions]


@router.post("", response_model=SessionPublic, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreateRequest, user: CurrentUser, store: Store
) -> SessionPublic:
    session = await store.create_session(
        user_id=user.id,
        title=payload.title or "新会话",
        model=payload.model or "",
        sandbox_enabled=payload.sandbox_enabled,
    )
    return SessionPublic.model_validate(session)


@router.get("/{session_id}", response_model=SessionPublic)
async def get_session(session_id: str, user: CurrentUser, store: Store) -> SessionPublic:
    session = await _get_owned(session_id, user, store)
    return SessionPublic.model_validate(session)


@router.patch("/{session_id}", response_model=SessionPublic)
async def update_session(
    session_id: str, payload: SessionUpdateRequest, user: CurrentUser, store: Store
) -> SessionPublic:
    session = await _get_owned(session_id, user, store)
    await store.update_session(
        session,
        title=payload.title,
        sandbox_enabled=payload.sandbox_enabled,
    )
    return SessionPublic.model_validate(session)


@router.delete("/{session_id}", response_model=MessageResponse)
async def delete_session(session_id: str, user: CurrentUser, store: Store) -> MessageResponse:
    session = await _get_owned(session_id, user, store)
    await store.delete_session(session)
    return MessageResponse(message="会话已删除")


@router.get("/{session_id}/messages", response_model=list[MessagePublic])
async def list_messages(session_id: str, user: CurrentUser, store: Store) -> list[MessagePublic]:
    await _get_owned(session_id, user, store)
    messages = await store.list_messages(session_id)
    return [MessagePublic.model_validate(m) for m in messages]


@router.get("/{session_id}/attachments", response_model=list[AttachmentPublic])
async def list_session_attachments(
    session_id: str, user: CurrentUser, store: Store
) -> list[AttachmentPublic]:
    """本会话的附件清单（会话级资源，按上传时间正序）。

    附件正文每轮都会注入 system 的「本会话文件」区，所以前端需要一份**当前**清单：
    历史消息里的附件快照是「当初发过什么」，不会随删除而变，两者不能混用。
    用户在会话内删掉某个附件后，这里立即不再返回它（正文也不再注入模型）。
    """
    await _get_owned(session_id, user, store)
    attachments = await store.list_session_attachments(session_id)
    return [AttachmentPublic.model_validate(a) for a in attachments]
