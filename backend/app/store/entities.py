"""领域实体：仓储对上层暴露的统一数据模型。

内存实现与 SQL 实现**都返回这里定义的 dataclass**，
从而保证 API 层 / Agent 层在切换存储后端时行为完全一致。
字段与《技术设计文档》5.2 节表结构对齐，时间统一为 **UTC aware**。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    """带时区的 UTC 当前时间（领域层统一口径）。"""
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class User:
    id: str
    username: str
    email: str
    password_hash: str
    role: str = "user"
    sandbox_enabled: bool = False
    email_verified: bool = False
    status: str = "active"
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass
class Session:
    id: str
    user_id: str
    title: str = "新会话"
    model: str = ""
    sandbox_enabled: bool = False
    summary: str = ""
    #: 摘要已覆盖到的 `messages.seq`（-1 表示尚未压缩过任何历史）
    summary_upto_seq: int = -1
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    deleted_at: Optional[datetime] = None


@dataclass
class Message:
    id: str
    session_id: str
    role: str
    content: str = ""
    seq: int = 0
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    #: 本条消息携带的附件元数据快照（对话附件，见 `Attachment.to_meta`）：
    #: 只存元数据，正文放在 attachments 表，改由 `GET /api/v1/attachments/{id}` 取
    attachments: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Attachment:
    """对话附件：用户在聊天输入框随消息上传的文件。

    - `text`：解析出的纯文本，发送时注入模型上下文（截断见 `attachment_max_chars`）；
    - `message_id`：为空表示「已上传但还没随消息发出」，超过 TTL 由
      `purge_stale_attachments` 清理，避免用户选完文件又没发送时留下垃圾数据；
    - `session_id`：绑定时回填，便于按会话排查问题。
    """

    id: str
    user_id: str
    filename: str
    file_type: str
    content_type: str = ""
    size_bytes: int = 0
    char_count: int = 0
    text: str = ""
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)

    @property
    def bound(self) -> bool:
        """是否已随某条消息发出。"""
        return bool(self.message_id)

    def to_meta(self) -> dict[str, Any]:
        """写进消息 `attachments` 字段的元数据快照（不含正文）。"""
        return {
            "id": self.id,
            "filename": self.filename,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "char_count": self.char_count,
        }


@dataclass
class ResetToken:
    token_hash: str
    user_id: str
    expires_at: datetime
    created_at: datetime = field(default_factory=utcnow)
    used_at: Optional[datetime] = None


@dataclass
class ConfigItem:
    """一条系统配置。

    `encrypted=True` 时 `value` 为 `app.core.crypto` 产出的密文，
    仓储层只负责原样存取，加解密由运行时配置服务统一处理。
    """

    key: str
    value: str = ""
    encrypted: bool = False
    updated_by: Optional[str] = None
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class KnowledgeDocument:
    """知识库文档（M4）。

    `status` 取值：`ready`（已解析入库）、`failed`（解析失败，原因见 `error_message`）。
    """

    id: str
    user_id: str
    filename: str
    file_type: str
    size_bytes: int = 0
    status: str = "ready"
    chunk_count: int = 0
    char_count: int = 0
    error_message: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    deleted_at: Optional[datetime] = None


@dataclass
class EmbeddingChunk:
    """可检索的文本块（M4）。

    - `source="knowledge"`：来自知识库文档，`document_id` 必填；
    - `source="memory"`：来自长期记忆（后续里程碑接入）。

    `embedding` 为浮点向量，由 `app/core/embedding.py` 产出；为空表示该块尚未向量化
    （如上传时嵌入后端为 `none`），可通过 `POST /api/v1/knowledge/reindex` 回填。
    """

    id: str
    user_id: str
    content: str
    source: str = "knowledge"
    document_id: Optional[str] = None
    seq: int = 0
    embedding: Optional[list[float]] = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class AuditLog:
    """工具调用审计记录（M5，《技术设计文档》5.2 `tool_audit_logs`）。

    - `args` / `result` 为 JSON 文本：参数已脱敏截断，结果只存摘要；
    - `status`：`success`（成功）/ `error`（工具报错）/ `timeout`（超时）/
      `rejected`（用户拒绝或确认超时）/ `blocked`（沙箱或知识库未开启导致未执行）；
    - `approved`：是否需要并经过了用户人工确认（M3 的人工确认链路）。
    """

    id: str
    user_id: str
    session_id: str
    tool_name: str
    args: str = "{}"
    result: str = "{}"
    status: str = "success"
    duration_ms: int = 0
    approved: bool = False
    created_at: datetime = field(default_factory=utcnow)


__all__ = [
    "Attachment",
    "AuditLog",
    "ConfigItem",
    "EmbeddingChunk",
    "KnowledgeDocument",
    "Message",
    "ResetToken",
    "Session",
    "User",
    "new_id",
    "utcnow",
]
