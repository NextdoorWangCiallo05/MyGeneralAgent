"""对话附件接口：上传 / 查看原文 / 删除。

用户在聊天输入框选择的文件先上传到这里拿到 `attachment_id`，
随后由 `POST /api/v1/chat` 的 `attachment_ids` 随消息发出（见 app/api/chat.py）。

生命周期：
- 上传后 `message_id` 为空，表示「还没发出去」；
- 随消息发出时绑定到该条消息，历史消息里保留元数据快照；
- 上传后始终没发送的附件超过 `attachment_ttl_seconds` 会被清理，
  清理动作挂在上传路径上做节流（见 `_purge_stale`）。

解析复用知识库的文档解析器（`app/core/document_parser.py`），
因此支持格式与 `ATTACHMENT_ALLOWED_TYPES` 一致。
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, File, UploadFile, status

from app.api.deps import CurrentUser, Store, rate_limited
from app.config import Settings, get_settings
from app.core.document_parser import detect_file_type, ensure_filename_length, parse_document
from app.core.errors import (
    AttachmentNotFoundError,
    DocumentParseError,
    DocumentTooLargeError,
    UnsupportedDocumentError,
)
from app.schemas.attachment import AttachmentDetail, AttachmentPublic
from app.schemas.auth import MessageResponse
from app.store.entities import Attachment, User, utcnow

logger = logging.getLogger("app.api.attachments")

router = APIRouter(prefix="/attachments", tags=["attachments"])

#: 「查看原文」返回的最大字符数（与送模型的上限解耦，前端预览可以更完整）
_PREVIEW_MAX_CHARS = 100_000
#: 过期附件清理的最小间隔（秒），避免每次上传都扫一遍
_PURGE_INTERVAL_SECONDS = 300.0
_last_purge_at: float = 0.0


async def _get_owned_attachment(attachment_id: str, user: User, store: Store) -> Attachment:
    """按 id 取附件并校验归属，避免越权读取他人文件内容。"""
    attachment = await store.get_attachment(attachment_id)
    if attachment is None or attachment.user_id != user.id:
        raise AttachmentNotFoundError()
    return attachment


async def _purge_stale(store: Store, settings: Settings) -> None:
    """清理过期且未发送的附件（best-effort，失败不影响上传）。"""
    global _last_purge_at
    now = time.monotonic()
    if now - _last_purge_at < _PURGE_INTERVAL_SECONDS:
        return
    _last_purge_at = now
    try:
        removed = await store.purge_stale_attachments(
            before=utcnow() - timedelta(seconds=settings.attachment_ttl_seconds)
        )
    except Exception:  # noqa: BLE001 - 清理失败不应影响正常上传
        logger.exception("清理过期附件失败")
        return
    if removed:
        logger.info("已清理 %s 个超时未发送的附件", removed)


@router.post(
    "",
    response_model=AttachmentPublic,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limited("upload"))],
)
async def upload_attachment(
    user: CurrentUser,
    store: Store,
    file: UploadFile = File(...),
) -> AttachmentPublic:
    """上传附件：解析为纯文本后暂存，等待随消息发送。"""
    settings = get_settings()
    filename = ensure_filename_length((file.filename or "").strip() or "未命名文件")
    file_type = detect_file_type(filename)

    allowed = set(settings.attachment_allowed_type_list)
    if allowed and file_type not in allowed:
        raise UnsupportedDocumentError(f"仅支持 {'、'.join(sorted(allowed))} 类型的附件")

    data = await file.read()
    if not data:
        raise DocumentParseError("文件内容为空")
    if len(data) > settings.attachment_max_file_bytes:
        limit_mb = settings.attachment_max_file_bytes / (1024 * 1024)
        raise DocumentTooLargeError(f"附件不能超过 {limit_mb:g} MB")

    text = parse_document(filename, data, file_type=file_type).strip()
    if not text:
        raise DocumentParseError("文件解析后没有可用的文本内容")

    await _purge_stale(store, settings)

    attachment = await store.add_attachment(
        user_id=user.id,
        filename=filename,
        file_type=file_type,
        content_type=file.content_type or "",
        size_bytes=len(data),
        text=text,
    )
    logger.info(
        "附件已上传",
        extra={
            "user_id": user.id,
            "attachment_id": attachment.id,
            "file_type": file_type,
            "size_bytes": len(data),
        },
    )
    return AttachmentPublic.model_validate(attachment)


@router.get("/{attachment_id}", response_model=AttachmentDetail)
async def get_attachment(
    attachment_id: str, user: CurrentUser, store: Store
) -> AttachmentDetail:
    """查看附件详情：元数据 + 解析后的正文（供前端「查看原文」）。"""
    attachment = await _get_owned_attachment(attachment_id, user, store)
    preview = attachment.text[:_PREVIEW_MAX_CHARS]
    return AttachmentDetail(
        id=attachment.id,
        filename=attachment.filename,
        file_type=attachment.file_type,
        content_type=attachment.content_type,
        size_bytes=attachment.size_bytes,
        char_count=attachment.char_count,
        created_at=attachment.created_at,
        bound=attachment.bound,
        text=preview,
        truncated=len(attachment.text) > len(preview),
    )


@router.delete("/{attachment_id}", response_model=MessageResponse)
async def delete_attachment(
    attachment_id: str, user: CurrentUser, store: Store
) -> MessageResponse:
    """删除附件。

    已随消息发出的附件也允许删除：历史消息里仍保留元数据快照，只是正文不再可取。
    """
    attachment = await _get_owned_attachment(attachment_id, user, store)
    await store.delete_attachment(attachment)
    return MessageResponse(message="附件已删除")
