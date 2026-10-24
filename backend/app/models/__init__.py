"""ORM 模型：与《技术设计文档》5.2 节表结构对齐。

约定：
- 主键统一使用 `String(36)` 存放 UUID 字符串，避免各数据库 UUID 类型差异；
- 时间统一以 **UTC 朴素时间**（naive UTC）入库：MySQL 的 DATETIME 不保存时区，
  跨方言行为一致；领域层在读取时再补回 `tzinfo=UTC`；
- `JSON` 为 SQLAlchemy 通用类型，在 MySQL 映射为 JSON、SQLite 映射为 TEXT；
- 枚举值（role / status）用短字符串表示，避免 MySQL ENUM 的迁移成本。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.vector import EmbeddingVector, embedding_dim

#: 大文本列：MySQL 的 `TEXT` 上限是 65535 **字节**（不是字符），一个 4 万汉字的附件正文
#: 就要约 120 KB，插入时会报 `DataError (1406) Data too long for column ...`。
#: 附件正文、消息正文（可能拼接多个附件的 2 万字符）、向量 JSON 都可能超过这个量级，
#: 因此显式声明为 `LONGTEXT`；SQLite 等没有该限制的方言仍退化为普通 `TEXT`。
LONG_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")


def utcnow() -> datetime:
    """朴素 UTC 时间（见模块约定）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _uuid() -> str:
    return str(uuid.uuid4())


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    sandbox_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新会话")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 摘要覆盖到的 messages.seq（-1 = 未压缩过），见 app/core/summary.py
    summary_upto_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sandbox_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session_seq", "session_id", "seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 消息正文：附件内容会被拼进本轮用户消息，5 个附件（各 2 万字符）就可能超过 MySQL TEXT 上限
    content: Mapped[str] = mapped_column(LONG_TEXT, nullable=False, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 本条消息携带的附件元数据快照（对话附件，正文见 attachments 表）
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AttachmentRow(Base):
    """对话附件：用户在聊天输入框随消息上传的文件。

    - `text` 存解析后的纯文本，送模型时按 `attachment_max_chars` 截断；
    - `message_id` 为空表示「已上传但尚未随消息发出」，超过 TTL 由
      `purge_stale_attachments` 清理；
    - 附件正文单独成表（而非塞进 `messages.attachments`），避免消息表被大文本撑大。
    """

    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachments_user_message", "user_id", "message_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 解析出的完整正文：上游只按 ATTACHMENT_MAX_FILE_BYTES(5MB) 限文件大小，
    #: 纯文本最多可达数百万字符，必须用 LONGTEXT 才能装下
    text: Mapped[str] = mapped_column(LONG_TEXT, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class PasswordResetTokenRow(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class SystemConfigRow(Base):
    """系统配置（管理员后台维护）。

    与其它表不同，本表没有独立的 `id` 列：`key` 即业务主键，
    与《技术设计文档》5.2 节的表结构保持一致。
    敏感项（如 `deepseek_api_key`）以密文存入 `value`，由 `encrypted` 标记。
    """

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class KnowledgeDocumentRow(Base):
    """知识库文档（M4）。"""

    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EmbeddingChunkRow(Base):
    """可检索文本块（M4），对应《技术设计文档》5.2 节的 `embeddings` 表。

    向量列由 `EmbeddingVector` 按方言落地：PostgreSQL 为原生 `vector(N)`
    （`<=>` + HNSW 索引，数据库内 top-k）；MySQL / SQLite 仍是 JSON 文本，
    检索退化为应用层余弦相似度（见 `app/store/sql.py` 的 `search_chunks`）。
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        Index("ix_embeddings_user_source", "user_id", "source"),
        UniqueConstraint("document_id", "seq", name="uq_embeddings_document_seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="knowledge")
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: PostgreSQL = vector(EMBEDDING_DIM)；MySQL = LONGTEXT（JSON 文本）
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingVector(embedding_dim()), nullable=True
    )
    chunk_meta: Mapped[dict | None] = mapped_column("meta", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ToolAuditLogRow(Base):
    """工具调用审计（M5），对应《技术设计文档》5.2 节的 `tool_audit_logs` 表。

    相比设计文档额外增加了 `user_id`（以便按用户直接过滤，无需 join sessions）
    与 `approved`（M3 人工确认结果：是否需要并经过了用户确认）。
    `args` / `result` 存 JSON 文本，参数已脱敏、结果只存摘要（见 app/core/audit.py）。
    """

    __tablename__ = "tool_audit_logs"
    __table_args__ = (Index("ix_tool_audit_session_created", "session_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    args: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    #: success / error / timeout / rejected / blocked
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


__all__ = [
    "AttachmentRow",
    "EmbeddingChunkRow",
    "KnowledgeDocumentRow",
    "MessageRow",
    "PasswordResetTokenRow",
    "SessionRow",
    "SystemConfigRow",
    "ToolAuditLogRow",
    "UserRow",
    "utcnow",
]
