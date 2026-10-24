"""SQLAlchemy 异步仓储（MySQL 8+ 为主，SQLite 用于测试）。

实现要点：
- 每次调用自建一个事务作用域（`_scope`），提交后把 ORM 行转换为领域实体，
  因此上层拿到的对象与内存实现完全同构，不受 session 生命周期影响；
- 时间入库存 naive UTC，读出补 `tzinfo=UTC`（见 `app.models` 约定）；
- `update_session` / `delete_session` 按主键重查后再写入，兼容传入的游离实体。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import bindparam, delete, func, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.vector_math import score_all
from app.models import (
    AttachmentRow,
    EmbeddingChunkRow,
    KnowledgeDocumentRow,
    MessageRow,
    PasswordResetTokenRow,
    SessionRow,
    SystemConfigRow,
    ToolAuditLogRow,
    UserRow,
)
from app.models import utcnow as db_utcnow
from app.models.vector import vector_literal
from app.store.base import AbstractStore
from app.store.entities import (
    Attachment,
    AuditLog,
    ChunkMatch,
    ConfigItem,
    EmbeddingChunk,
    KnowledgeDocument,
    Message,
    ResetToken,
    Session,
    User,
    new_id,
    utcnow,
)

logger = logging.getLogger("app.store.sql")


# ---------------------------------------------------------------- 时间转换
def _to_db(value: Optional[datetime]) -> Optional[datetime]:
    """领域层 aware UTC → 数据库 naive UTC。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _from_db(value: Optional[datetime]) -> Optional[datetime]:
    """数据库 naive UTC → 领域层 aware UTC。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------- 行 → 实体
def _to_user(row: UserRow) -> User:
    return User(
        id=row.id,
        username=row.username,
        email=row.email,
        password_hash=row.password_hash,
        role=row.role,
        sandbox_enabled=row.sandbox_enabled,
        email_verified=row.email_verified,
        status=row.status,
        created_at=_from_db(row.created_at) or utcnow(),
        updated_at=_from_db(row.updated_at) or utcnow(),
    )


def _to_session(row: SessionRow) -> Session:
    return Session(
        id=row.id,
        user_id=row.user_id,
        title=row.title,
        model=row.model,
        sandbox_enabled=row.sandbox_enabled,
        summary=row.summary or "",
        summary_upto_seq=row.summary_upto_seq if row.summary_upto_seq is not None else -1,
        created_at=_from_db(row.created_at) or utcnow(),
        updated_at=_from_db(row.updated_at) or utcnow(),
        deleted_at=_from_db(row.deleted_at),
    )


def _to_message(row: MessageRow) -> Message:
    return Message(
        id=row.id,
        session_id=row.session_id,
        role=row.role,
        content=row.content or "",
        seq=row.seq,
        tool_calls=row.tool_calls,
        tool_call_id=row.tool_call_id,
        attachments=list(row.attachments or []),
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        created_at=_from_db(row.created_at) or utcnow(),
    )


def _to_attachment(row: AttachmentRow) -> Attachment:
    return Attachment(
        id=row.id,
        user_id=row.user_id,
        filename=row.filename,
        file_type=row.file_type,
        content_type=row.content_type or "",
        size_bytes=row.size_bytes or 0,
        char_count=row.char_count or 0,
        text=row.text or "",
        session_id=row.session_id,
        message_id=row.message_id,
        created_at=_from_db(row.created_at) or utcnow(),
    )


def _to_token(row: PasswordResetTokenRow) -> ResetToken:
    return ResetToken(
        token_hash=row.token_hash,
        user_id=row.user_id,
        expires_at=_from_db(row.expires_at) or utcnow(),
        created_at=_from_db(row.created_at) or utcnow(),
        used_at=_from_db(row.used_at),
    )


def _to_audit_log(row: ToolAuditLogRow) -> AuditLog:
    return AuditLog(
        id=row.id,
        user_id=row.user_id,
        session_id=row.session_id,
        tool_name=row.tool_name,
        args=row.args or "{}",
        result=row.result or "{}",
        status=row.status,
        duration_ms=row.duration_ms or 0,
        approved=bool(row.approved),
        created_at=_from_db(row.created_at) or utcnow(),
    )


def _to_config(row: SystemConfigRow) -> ConfigItem:
    return ConfigItem(
        key=row.key,
        value=row.value or "",
        encrypted=bool(row.encrypted),
        updated_by=row.updated_by,
        updated_at=_from_db(row.updated_at) or utcnow(),
    )


def _to_document(row: KnowledgeDocumentRow) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=row.id,
        user_id=row.user_id,
        filename=row.filename,
        file_type=row.file_type,
        size_bytes=row.size_bytes or 0,
        status=row.status,
        chunk_count=row.chunk_count or 0,
        char_count=row.char_count or 0,
        error_message=row.error_message or "",
        created_at=_from_db(row.created_at) or utcnow(),
        updated_at=_from_db(row.updated_at) or utcnow(),
        deleted_at=_from_db(row.deleted_at),
    )


def _to_chunk(row: EmbeddingChunkRow) -> EmbeddingChunk:
    """向量列由 `EmbeddingVector` 按方言解码：PG 是 list，MySQL/SQLite 是 JSON 文本。"""
    embedding: Optional[list[float]] = None
    if row.embedding:
        value = row.embedding
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = None
        if isinstance(value, (list, tuple)):
            embedding = [float(item) for item in value]
    return EmbeddingChunk(
        id=row.id,
        user_id=row.user_id,
        content=row.content or "",
        source=row.source,
        document_id=row.document_id,
        seq=row.seq,
        embedding=embedding,
        meta=row.chunk_meta or {},
        created_at=_from_db(row.created_at) or utcnow(),
    )


class SqlStore(AbstractStore):
    """基于 SQLAlchemy AsyncSession 的仓储。"""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        bind = getattr(sessionmaker, "kw", {}).get("bind")
        self._dialect = getattr(getattr(bind, "dialect", None), "name", "sqlite")

    @property
    def supports_native_vectors(self) -> bool:
        """当前方言是否具备原生向量检索能力（PostgreSQL + pgvector）。"""
        return self._dialect == "postgresql"

    @asynccontextmanager
    async def _scope(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # ---------------------------------------------------------------- 用户
    async def create_user(
        self,
        *,
        username: str,
        email: str,
        password_hash: str,
        role: str = "user",
    ) -> User:
        async with self._scope() as session:
            row = UserRow(
                id=new_id(),
                username=username,
                email=email.lower(),
                password_hash=password_hash,
                role=role,
            )
            session.add(row)
            await session.flush()
            return _to_user(row)

    async def get_user(self, user_id: str) -> Optional[User]:
        async with self._scope() as session:
            row = await session.get(UserRow, user_id)
            return _to_user(row) if row else None

    async def get_user_by_username(self, username: str) -> Optional[User]:
        stmt = (
            select(UserRow)
            .where(func.lower(UserRow.username) == username.lower())
            .limit(1)
        )
        async with self._scope() as session:
            row = await session.scalar(stmt)
            return _to_user(row) if row else None

    async def get_user_by_email(self, email: str) -> Optional[User]:
        stmt = select(UserRow).where(func.lower(UserRow.email) == email.lower()).limit(1)
        async with self._scope() as session:
            row = await session.scalar(stmt)
            return _to_user(row) if row else None

    async def get_user_by_login(self, identifier: str) -> Optional[User]:
        """登录时支持用户名或邮箱（单条查询完成）。"""
        lowered = identifier.lower()
        stmt = (
            select(UserRow)
            .where(or_(func.lower(UserRow.username) == lowered, func.lower(UserRow.email) == lowered))
            .limit(1)
        )
        async with self._scope() as session:
            row = await session.scalar(stmt)
            return _to_user(row) if row else None

    async def list_users(self) -> list[User]:
        stmt = select(UserRow).order_by(UserRow.created_at.asc())
        async with self._scope() as session:
            rows = (await session.scalars(stmt)).all()
            return [_to_user(r) for r in rows]

    async def update_password(self, user_id: str, password_hash: str) -> None:
        async with self._scope() as session:
            row = await session.get(UserRow, user_id)
            if row is None:
                return
            row.password_hash = password_hash
            row.updated_at = db_utcnow()

    # ---------------------------------------------------------------- 会话
    async def create_session(
        self,
        *,
        user_id: str,
        title: str = "新会话",
        model: str = "",
        sandbox_enabled: bool = False,
    ) -> Session:
        async with self._scope() as session:
            row = SessionRow(
                id=new_id(),
                user_id=user_id,
                title=title,
                model=model,
                sandbox_enabled=sandbox_enabled,
            )
            session.add(row)
            await session.flush()
            return _to_session(row)

    async def get_session(self, session_id: str) -> Optional[Session]:
        stmt = select(SessionRow).where(
            SessionRow.id == session_id, SessionRow.deleted_at.is_(None)
        )
        async with self._scope() as session:
            row = await session.scalar(stmt)
            return _to_session(row) if row else None

    async def list_sessions(self, user_id: str) -> list[Session]:
        stmt = (
            select(SessionRow)
            .where(SessionRow.user_id == user_id, SessionRow.deleted_at.is_(None))
            .order_by(SessionRow.updated_at.desc())
        )
        async with self._scope() as session:
            rows = (await session.scalars(stmt)).all()
            return [_to_session(r) for r in rows]

    async def update_session(self, session: Session, **fields: Any) -> Session:
        async with self._scope() as scope:
            row = await scope.get(SessionRow, session.id)
            if row is None:
                return session
            for key, value in fields.items():
                if value is not None and hasattr(row, key):
                    setattr(row, key, value)
            row.updated_at = db_utcnow()
            await scope.flush()
            return _to_session(row)

    async def delete_session(self, session: Session) -> None:
        async with self._scope() as scope:
            row = await scope.get(SessionRow, session.id)
            if row is not None:
                row.deleted_at = db_utcnow()

    # ---------------------------------------------------------------- 消息
    async def add_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str = "",
        tool_calls: Optional[list[dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> Message:
        async with self._scope() as session:
            next_seq = await session.scalar(
                select(func.coalesce(func.max(MessageRow.seq), -1) + 1).where(
                    MessageRow.session_id == session_id
                )
            )
            row = MessageRow(
                id=new_id(),
                session_id=session_id,
                role=role,
                content=content,
                seq=int(next_seq or 0),
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                attachments=list(attachments or []),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            session.add(row)

            parent = await session.get(SessionRow, session_id)
            if parent is not None:
                parent.updated_at = db_utcnow()

            await session.flush()
            return _to_message(row)

    async def list_messages(self, session_id: str) -> list[Message]:
        stmt = (
            select(MessageRow)
            .where(MessageRow.session_id == session_id)
            .order_by(MessageRow.seq.asc(), MessageRow.created_at.asc())
        )
        async with self._scope() as session:
            rows = (await session.scalars(stmt)).all()
            return [_to_message(r) for r in rows]

    async def delete_messages_from(self, session_id: str, seq: int) -> int:
        async with self._scope() as session:
            result = await session.execute(
                delete(MessageRow).where(
                    MessageRow.session_id == session_id, MessageRow.seq >= seq
                )
            )
            return int(result.rowcount or 0)

    # ---------------------------------------------------------------- 对话附件
    async def add_attachment(
        self,
        *,
        user_id: str,
        filename: str,
        file_type: str,
        content_type: str = "",
        size_bytes: int = 0,
        text: str = "",
        session_id: Optional[str] = None,
    ) -> Attachment:
        async with self._scope() as session:
            row = AttachmentRow(
                id=new_id(),
                user_id=user_id,
                session_id=session_id,
                filename=filename,
                file_type=file_type,
                content_type=content_type,
                size_bytes=size_bytes,
                char_count=len(text),
                text=text,
            )
            session.add(row)
            await session.flush()
            return _to_attachment(row)

    async def get_attachment(self, attachment_id: str) -> Optional[Attachment]:
        async with self._scope() as session:
            row = await session.get(AttachmentRow, attachment_id)
            return _to_attachment(row) if row is not None else None

    async def list_attachments(self, attachment_ids: list[str]) -> list[Attachment]:
        if not attachment_ids:
            return []
        stmt = select(AttachmentRow).where(AttachmentRow.id.in_(attachment_ids))
        async with self._scope() as session:
            rows = (await session.scalars(stmt)).all()
        by_id = {row.id: _to_attachment(row) for row in rows}
        # 保持入参顺序，与内存实现一致
        return [by_id[item] for item in attachment_ids if item in by_id]

    async def list_session_attachments(self, session_id: str) -> list[Attachment]:
        stmt = (
            select(AttachmentRow)
            .where(AttachmentRow.session_id == session_id)
            # created_at 精度有限，同一秒上传的多个文件按 id 兜底排序，保证顺序稳定
            .order_by(AttachmentRow.created_at.asc(), AttachmentRow.id.asc())
        )
        async with self._scope() as session:
            rows = (await session.scalars(stmt)).all()
        return [_to_attachment(row) for row in rows]

    async def bind_attachments(
        self,
        attachment_ids: list[str],
        *,
        message_id: str,
        session_id: str,
    ) -> int:
        if not attachment_ids:
            return 0
        async with self._scope() as session:
            result = await session.execute(
                update(AttachmentRow)
                .where(AttachmentRow.id.in_(attachment_ids))
                .values(message_id=message_id, session_id=session_id)
            )
            return int(result.rowcount or 0)

    async def delete_attachment(self, attachment: Attachment) -> None:
        async with self._scope() as session:
            row = await session.get(AttachmentRow, attachment.id)
            if row is not None:
                await session.delete(row)

    async def purge_stale_attachments(self, *, before: datetime) -> int:
        async with self._scope() as session:
            result = await session.execute(
                delete(AttachmentRow).where(
                    AttachmentRow.message_id.is_(None),
                    AttachmentRow.created_at < _to_db(before),
                )
            )
            return int(result.rowcount or 0)

    # ---------------------------------------------------------------- 重置令牌
    async def create_reset_token(self, user_id: str, token_hash: str, ttl_minutes: int) -> ResetToken:
        async with self._scope() as session:
            row = PasswordResetTokenRow(
                id=new_id(),
                user_id=user_id,
                token_hash=token_hash,
                expires_at=_to_db(utcnow() + timedelta(minutes=ttl_minutes)),
            )
            session.add(row)
            await session.flush()
            return _to_token(row)

    async def get_reset_token(self, token_hash: str) -> Optional[ResetToken]:
        stmt = select(PasswordResetTokenRow).where(
            PasswordResetTokenRow.token_hash == token_hash
        )
        async with self._scope() as session:
            row = await session.scalar(stmt)
            return _to_token(row) if row else None

    async def consume_reset_token(self, token_hash: str) -> Optional[ResetToken]:
        """行级锁 + 一次性消费，避免并发重复使用。"""
        stmt = (
            select(PasswordResetTokenRow)
            .where(PasswordResetTokenRow.token_hash == token_hash)
            .with_for_update()
        )
        async with self._scope() as session:
            row = await session.scalar(stmt)
            if row is None or row.used_at is not None:
                return None
            if row.expires_at < db_utcnow():
                return None
            row.used_at = db_utcnow()
            await session.flush()
            return _to_token(row)

    async def invalidate_reset_tokens(self, user_id: str) -> None:
        stmt = (
            update(PasswordResetTokenRow)
            .where(
                PasswordResetTokenRow.user_id == user_id,
                PasswordResetTokenRow.used_at.is_(None),
            )
            .values(used_at=db_utcnow())
        )
        async with self._scope() as session:
            await session.execute(stmt)

    # ---------------------------------------------------------------- 系统配置
    async def list_configs(self) -> list[ConfigItem]:
        stmt = select(SystemConfigRow).order_by(SystemConfigRow.key.asc())
        async with self._scope() as session:
            rows = (await session.scalars(stmt)).all()
            return [_to_config(r) for r in rows]

    async def get_config(self, key: str) -> Optional[ConfigItem]:
        async with self._scope() as session:
            row = await session.get(SystemConfigRow, key)
            return _to_config(row) if row else None

    async def set_config(
        self,
        key: str,
        value: str,
        *,
        encrypted: bool = False,
        updated_by: Optional[str] = None,
    ) -> ConfigItem:
        """按主键 upsert 一条配置。"""
        async with self._scope() as session:
            row = await session.get(SystemConfigRow, key)
            if row is None:
                row = SystemConfigRow(
                    key=key,
                    value=value,
                    encrypted=encrypted,
                    updated_by=updated_by,
                )
                session.add(row)
            else:
                row.value = value
                row.encrypted = encrypted
                row.updated_by = updated_by
            row.updated_at = db_utcnow()
            await session.flush()
            return _to_config(row)

    async def delete_config(self, key: str) -> None:
        async with self._scope() as session:
            row = await session.get(SystemConfigRow, key)
            if row is not None:
                await session.delete(row)

    # ---------------------------------------------------------------- 知识库
    async def create_document(
        self,
        *,
        user_id: str,
        filename: str,
        file_type: str,
        size_bytes: int = 0,
        status: str = "ready",
    ) -> KnowledgeDocument:
        async with self._scope() as session:
            row = KnowledgeDocumentRow(
                id=new_id(),
                user_id=user_id,
                filename=filename,
                file_type=file_type,
                size_bytes=size_bytes,
                status=status,
            )
            session.add(row)
            await session.flush()
            return _to_document(row)

    async def get_document(self, document_id: str) -> Optional[KnowledgeDocument]:
        stmt = select(KnowledgeDocumentRow).where(
            KnowledgeDocumentRow.id == document_id,
            KnowledgeDocumentRow.deleted_at.is_(None),
        )
        async with self._scope() as session:
            row = await session.scalar(stmt)
            return _to_document(row) if row else None

    async def list_documents(self, user_id: str) -> list[KnowledgeDocument]:
        stmt = (
            select(KnowledgeDocumentRow)
            .where(
                KnowledgeDocumentRow.user_id == user_id,
                KnowledgeDocumentRow.deleted_at.is_(None),
            )
            .order_by(KnowledgeDocumentRow.created_at.desc())
        )
        async with self._scope() as session:
            rows = (await session.scalars(stmt)).all()
            return [_to_document(r) for r in rows]

    async def update_document(
        self, document: KnowledgeDocument, **fields: Any
    ) -> KnowledgeDocument:
        async with self._scope() as scope:
            row = await scope.get(KnowledgeDocumentRow, document.id)
            if row is None:
                return document
            for key, value in fields.items():
                if value is not None and hasattr(row, key):
                    setattr(row, key, value)
            row.updated_at = db_utcnow()
            await scope.flush()
            return _to_document(row)

    async def delete_document(self, document: KnowledgeDocument) -> None:
        async with self._scope() as scope:
            row = await scope.get(KnowledgeDocumentRow, document.id)
            if row is not None:
                row.deleted_at = db_utcnow()
            await scope.execute(
                delete(EmbeddingChunkRow).where(
                    EmbeddingChunkRow.document_id == document.id
                )
            )

    async def add_chunks(
        self,
        *,
        user_id: str,
        document_id: str,
        contents: list[str],
        source: str = "knowledge",
        embeddings: Optional[list[list[float]]] = None,
    ) -> int:
        if not contents:
            return 0
        async with self._scope() as session:
            # 文档内 seq 连续递增，重复导入同一文档时会追加而非覆盖。
            # 并发上传同一文档时，「读 max(seq) 再插入」会撞唯一约束
            # `uq_embeddings_document_seq`：先锁住文档行，把同一文档的写入串行化。
            # 锁粒度是单个文档，不同文档的上传互不阻塞。
            if self._dialect in {"mysql", "postgresql"}:
                await session.execute(
                    select(KnowledgeDocumentRow.id)
                    .where(KnowledgeDocumentRow.id == document_id)
                    .with_for_update()
                )
            start = await session.scalar(
                select(func.coalesce(func.max(EmbeddingChunkRow.seq), -1) + 1).where(
                    EmbeddingChunkRow.document_id == document_id
                )
            )
            base = int(start or 0)
            rows = [
                EmbeddingChunkRow(
                    id=new_id(),
                    user_id=user_id,
                    document_id=document_id,
                    source=source,
                    seq=base + index,
                    content=content,
                    embedding=(
                        list(embeddings[index])
                        if embeddings is not None
                        and index < len(embeddings)
                        and embeddings[index]
                        else None
                    ),
                    chunk_meta={"index": base + index},
                )
                for index, content in enumerate(contents)
            ]
            session.add_all(rows)
            await session.flush()
            return len(rows)

    async def list_chunks(
        self,
        user_id: str,
        *,
        document_id: Optional[str] = None,
        source: Optional[str] = None,
        only_embedded: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EmbeddingChunk]:
        stmt = select(EmbeddingChunkRow).where(EmbeddingChunkRow.user_id == user_id)
        if document_id is not None:
            stmt = stmt.where(EmbeddingChunkRow.document_id == document_id)
        if source is not None:
            stmt = stmt.where(EmbeddingChunkRow.source == source)
        if only_embedded:
            stmt = stmt.where(EmbeddingChunkRow.embedding.is_not(None))
        stmt = (
            stmt.order_by(EmbeddingChunkRow.document_id.asc(), EmbeddingChunkRow.seq.asc())
            .limit(limit)
            .offset(offset)
        )
        async with self._scope() as session:
            rows = (await session.scalars(stmt)).all()
            return [_to_chunk(r) for r in rows]

    async def count_chunks(
        self,
        user_id: str,
        *,
        source: Optional[str] = None,
        only_embedded: bool = False,
    ) -> int:
        stmt = select(func.count()).select_from(EmbeddingChunkRow).where(
            EmbeddingChunkRow.user_id == user_id
        )
        if source is not None:
            stmt = stmt.where(EmbeddingChunkRow.source == source)
        if only_embedded:
            stmt = stmt.where(EmbeddingChunkRow.embedding.is_not(None))
        async with self._scope() as session:
            return int(await session.scalar(stmt) or 0)

    async def update_chunk_embeddings(
        self, *, user_id: str, items: list[tuple[str, list[float]]]
    ) -> int:
        if not items:
            return 0
        payload = {chunk_id: list(vector) for chunk_id, vector in items if vector}
        if not payload:
            return 0
        async with self._scope() as session:
            # 先确认归属（同时得到「实际可更新条数」），再用 executemany 批量回填：
            # 一次 SELECT + 一次 executemany，避免逐行加载完整片段（含 content）。
            owned = (
                await session.scalars(
                    select(EmbeddingChunkRow.id).where(
                        EmbeddingChunkRow.user_id == user_id,
                        EmbeddingChunkRow.id.in_(list(payload)),
                    )
                )
            ).all()
            if not owned:
                return 0
            # 走 Core 表而非 ORM 实体：避免 SQLAlchemy 把 executemany 当作
            # 「按主键批量更新」，要求参数字典里带主键名
            await session.execute(
                update(EmbeddingChunkRow.__table__)
                .where(
                    EmbeddingChunkRow.__table__.c.user_id == user_id,
                    EmbeddingChunkRow.__table__.c.id == bindparam("chunk_id"),
                )
                .values(embedding=bindparam("embedding")),
                [
                    {"chunk_id": chunk_id, "embedding": payload[chunk_id]}
                    for chunk_id in owned
                ],
            )
            return len(owned)

    async def delete_chunks(self, *, user_id: str, document_id: str) -> int:
        stmt = delete(EmbeddingChunkRow).where(
            EmbeddingChunkRow.user_id == user_id,
            EmbeddingChunkRow.document_id == document_id,
        )
        async with self._scope() as session:
            result = await session.execute(stmt)
            return int(result.rowcount or 0)

    async def search_chunks(
        self,
        user_id: str,
        vector: list[float],
        *,
        source: str = "knowledge",
        top_k: int = 5,
        min_score: float = 0.0,
        candidate_limit: int = 2000,
    ) -> list[ChunkMatch]:
        if not vector:
            return []
        limit = max(1, top_k)
        if self._dialect == "postgresql":
            return await self._search_chunks_pg(
                user_id,
                vector,
                source=source,
                top_k=limit,
                min_score=min_score,
            )

        # MySQL / SQLite：无原生向量类型，取候选后在应用层打分（口径与内存实现一致）
        candidates = await self.list_chunks(
            user_id,
            source=source,
            only_embedded=True,
            limit=max(1, candidate_limit),
        )
        candidates = [
            chunk for chunk in candidates if chunk.embedding and len(chunk.embedding) == len(vector)
        ]
        if not candidates:
            return []
        scores = score_all(vector, [chunk.embedding or [] for chunk in candidates])
        ranked = sorted(zip(candidates, scores), key=lambda item: item[1], reverse=True)
        document_ids = {chunk.document_id for chunk, _ in ranked if chunk.document_id}
        documents = {
            document.id: document
            for document in await self.list_documents(user_id)
            if document.id in document_ids
        }
        matches: list[ChunkMatch] = []
        for chunk, score in ranked:
            if score < min_score:
                break
            document = documents.get(chunk.document_id or "")
            matches.append(
                ChunkMatch(
                    chunk=chunk,
                    score=score,
                    filename=document.filename if document is not None else "未知文档",
                )
            )
            if len(matches) >= limit:
                break
        return matches

    async def _search_chunks_pg(
        self,
        user_id: str,
        vector: list[float],
        *,
        source: str,
        top_k: int,
        min_score: float,
    ) -> list[ChunkMatch]:
        """PostgreSQL + pgvector：距离在数据库内计算，走 HNSW 索引。"""
        statement = text(
            """
            SELECT e.id AS id,
                   e.user_id AS user_id,
                   e.document_id AS document_id,
                   e.source AS source,
                   e.seq AS seq,
                   e.content AS content,
                   e.meta AS meta,
                   e.created_at AS created_at,
                   d.filename AS filename,
                   1 - (e.embedding <=> CAST(CAST(:embedding AS text) AS vector)) AS score
              FROM embeddings AS e
              LEFT JOIN knowledge_documents AS d ON d.id = e.document_id
             WHERE e.user_id = :user_id
               AND e.source = :source
               AND e.embedding IS NOT NULL
               AND 1 - (e.embedding <=> CAST(CAST(:embedding AS text) AS vector)) >= :min_score
             ORDER BY e.embedding <=> CAST(CAST(:embedding AS text) AS vector)
             LIMIT :top_k
            """
        )
        async with self._scope() as session:
            try:
                rows = (
                    await session.execute(
                        statement,
                        {
                            # 文本字面量 + CAST：原生 SQL 绕过 ORM 的向量绑定处理器
                            "embedding": vector_literal(vector),
                            "user_id": user_id,
                            "source": source,
                            "min_score": float(min_score),
                            "top_k": int(top_k),
                        },
                    )
                ).mappings()
                result = list(rows)
            except SQLAlchemyError:
                # 常见原因：向量维度与列定义不一致（切换嵌入后端后未重建列/索引）。
                # 检索是增强能力，失败时降级为空结果，不阻断对话。
                logger.warning(
                    "pgvector 检索失败（请检查 EMBEDDING_DIM 是否与列定义一致，并 force 重建索引）",
                    exc_info=True,
                )
                return []
        def _meta(value: Any) -> dict[str, Any]:
            # text() 不经过 ORM 的 JSON 类型处理器：asyncpg 下 JSON 列是字符串
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
            return value if isinstance(value, dict) else {}

        return [
            ChunkMatch(
                chunk=EmbeddingChunk(
                    id=row["id"],
                    user_id=row["user_id"],
                    content=row["content"] or "",
                    source=row["source"],
                    document_id=row["document_id"],
                    seq=row["seq"],
                    embedding=None,
                    meta=_meta(row["meta"]),
                    created_at=_from_db(row["created_at"]) or utcnow(),
                ),
                score=float(row["score"]),
                filename=row["filename"] or "未知文档",
            )
            for row in result
        ]

    async def list_embedded_document_ids(
        self, user_id: str, *, source: str = "knowledge"
    ) -> set[str]:
        stmt = (
            select(EmbeddingChunkRow.document_id)
            .where(
                EmbeddingChunkRow.user_id == user_id,
                EmbeddingChunkRow.source == source,
                EmbeddingChunkRow.embedding.is_not(None),
                EmbeddingChunkRow.document_id.is_not(None),
            )
            .distinct()
        )
        async with self._scope() as session:
            return {row for row in (await session.scalars(stmt)).all() if row}

    # ---------------------------------------------------------------- 审计（M5）
    async def add_audit_log(self, entry: AuditLog) -> AuditLog:
        row = ToolAuditLogRow(
            id=entry.id,
            user_id=entry.user_id,
            session_id=entry.session_id,
            tool_name=entry.tool_name,
            args=entry.args,
            result=entry.result,
            status=entry.status,
            duration_ms=entry.duration_ms,
            approved=entry.approved,
            created_at=_to_db(entry.created_at),
        )
        async with self._scope() as session:
            session.add(row)
            await session.flush()
            return _to_audit_log(row)

    def _audit_filters(
        self,
        user_id: Optional[str],
        *,
        session_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[Any]:
        conditions: list[Any] = []
        if user_id is not None:
            conditions.append(ToolAuditLogRow.user_id == user_id)
        if session_id:
            conditions.append(ToolAuditLogRow.session_id == session_id)
        if tool_name:
            conditions.append(ToolAuditLogRow.tool_name == tool_name)
        if status:
            conditions.append(ToolAuditLogRow.status == status)
        return conditions

    async def list_audit_logs(
        self,
        user_id: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLog]:
        stmt = (
            select(ToolAuditLogRow)
            .where(*self._audit_filters(user_id, session_id=session_id, tool_name=tool_name, status=status))
            .order_by(ToolAuditLogRow.created_at.desc(), ToolAuditLogRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._scope() as session:
            rows = (await session.scalars(stmt)).all()
            return [_to_audit_log(r) for r in rows]

    async def count_audit_logs(
        self,
        user_id: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        stmt = select(func.count()).select_from(ToolAuditLogRow).where(
            *self._audit_filters(user_id, session_id=session_id, tool_name=tool_name, status=status)
        )
        async with self._scope() as session:
            return int(await session.scalar(stmt) or 0)
