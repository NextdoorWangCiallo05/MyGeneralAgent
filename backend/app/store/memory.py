"""进程内内存存储（本地开发 / 测试用）。

- 领域对象来自 `app.store.entities`，与 SQL 实现完全一致；
- 所有写操作加锁，读操作返回对象引用供上层使用；
- 与 `SqlStore` 共同实现 `AbstractStore`，可按配置互换。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

from app.core.vector_math import score_all
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


class MemoryStore(AbstractStore):
    """线程/协程安全的内存仓储。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.users: dict[str, User] = {}
        self.sessions: dict[str, Session] = {}
        self.messages: dict[str, list[Message]] = {}
        self.reset_tokens: dict[str, ResetToken] = {}
        # 系统配置：key -> ConfigItem
        self.configs: dict[str, ConfigItem] = {}
        # 知识库文档：id -> KnowledgeDocument
        self.documents: dict[str, KnowledgeDocument] = {}
        # 可检索文本块：id -> EmbeddingChunk
        self.chunks: dict[str, EmbeddingChunk] = {}
        # 会话内消息顺序号（等价于 SQL 侧的 MAX(seq) + 1）
        self._seq: dict[str, int] = {}
        # 工具调用审计：id -> AuditLog
        self.audit_logs: dict[str, AuditLog] = {}
        # 对话附件：id -> Attachment
        self.attachments: dict[str, Attachment] = {}

    # ---------------------------------------------------------------- 用户
    async def create_user(
        self,
        *,
        username: str,
        email: str,
        password_hash: str,
        role: str = "user",
    ) -> User:
        async with self._lock:
            user = User(
                id=new_id(),
                username=username,
                email=email.lower(),
                password_hash=password_hash,
                role=role,
            )
            self.users[user.id] = user
            return user

    async def get_user(self, user_id: str) -> Optional[User]:
        return self.users.get(user_id)

    async def get_user_by_username(self, username: str) -> Optional[User]:
        lowered = username.lower()
        return next(
            (u for u in self.users.values() if u.username.lower() == lowered),
            None,
        )

    async def get_user_by_email(self, email: str) -> Optional[User]:
        lowered = email.lower()
        return next((u for u in self.users.values() if u.email == lowered), None)

    async def get_user_by_login(self, identifier: str) -> Optional[User]:
        """登录时支持用户名或邮箱。"""
        return await self.get_user_by_username(identifier) or await self.get_user_by_email(identifier)

    async def list_users(self) -> list[User]:
        return list(self.users.values())

    async def update_password(self, user_id: str, password_hash: str) -> None:
        async with self._lock:
            user = self.users.get(user_id)
            if user:
                user.password_hash = password_hash
                user.updated_at = utcnow()

    # ---------------------------------------------------------------- 会话
    async def create_session(
        self,
        *,
        user_id: str,
        title: str = "新会话",
        model: str = "",
        sandbox_enabled: bool = False,
    ) -> Session:
        async with self._lock:
            session = Session(
                id=new_id(),
                user_id=user_id,
                title=title,
                model=model,
                sandbox_enabled=sandbox_enabled,
            )
            self.sessions[session.id] = session
            self.messages[session.id] = []
            self._seq[session.id] = 0
            return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        session = self.sessions.get(session_id)
        if session and session.deleted_at is None:
            return session
        return None

    async def list_sessions(self, user_id: str) -> list[Session]:
        items = [
            s
            for s in self.sessions.values()
            if s.user_id == user_id and s.deleted_at is None
        ]
        return sorted(items, key=lambda s: s.updated_at, reverse=True)

    async def update_session(self, session: Session, **fields: Any) -> Session:
        async with self._lock:
            for key, value in fields.items():
                if value is not None and hasattr(session, key):
                    setattr(session, key, value)
            session.updated_at = utcnow()
            return session

    async def delete_session(self, session: Session) -> None:
        async with self._lock:
            session.deleted_at = utcnow()

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
        async with self._lock:
            seq = self._seq.get(session_id, 0)
            self._seq[session_id] = seq + 1
            session = self.sessions.get(session_id)
            if session:
                session.updated_at = utcnow()
            message = Message(
                id=new_id(),
                session_id=session_id,
                role=role,
                content=content,
                seq=seq,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                attachments=list(attachments or []),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            self.messages.setdefault(session_id, []).append(message)
            return message

    async def list_messages(self, session_id: str) -> list[Message]:
        return list(self.messages.get(session_id, []))

    async def delete_messages_from(self, session_id: str, seq: int) -> int:
        async with self._lock:
            items = self.messages.get(session_id)
            if not items:
                return 0
            kept = [item for item in items if item.seq < seq]
            removed = len(items) - len(kept)
            if removed:
                self.messages[session_id] = kept
                # 与 SqlStore 的 max(seq)+1 对齐：截断后序号从断点续接
                self._seq[session_id] = seq
            return removed

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
        async with self._lock:
            attachment = Attachment(
                id=new_id(),
                user_id=user_id,
                filename=filename,
                file_type=file_type,
                content_type=content_type,
                size_bytes=size_bytes,
                char_count=len(text),
                text=text,
                session_id=session_id,
            )
            self.attachments[attachment.id] = attachment
            return attachment

    async def get_attachment(self, attachment_id: str) -> Optional[Attachment]:
        return self.attachments.get(attachment_id)

    async def list_attachments(self, attachment_ids: list[str]) -> list[Attachment]:
        items: list[Attachment] = []
        for attachment_id in attachment_ids:
            item = self.attachments.get(attachment_id)
            if item is not None:
                items.append(item)
        return items

    async def list_session_attachments(self, session_id: str) -> list[Attachment]:
        items = [item for item in self.attachments.values() if item.session_id == session_id]
        items.sort(key=lambda item: (item.created_at, item.id))
        return items

    async def bind_attachments(
        self,
        attachment_ids: list[str],
        *,
        message_id: str,
        session_id: str,
    ) -> int:
        async with self._lock:
            updated = 0
            for attachment_id in attachment_ids:
                item = self.attachments.get(attachment_id)
                if item is None:
                    continue
                item.message_id = message_id
                item.session_id = session_id
                updated += 1
            return updated

    async def delete_attachment(self, attachment: Attachment) -> None:
        async with self._lock:
            self.attachments.pop(attachment.id, None)

    async def purge_stale_attachments(self, *, before: datetime) -> int:
        async with self._lock:
            stale = [
                attachment_id
                for attachment_id, item in self.attachments.items()
                if item.message_id is None and item.created_at < before
            ]
            for attachment_id in stale:
                self.attachments.pop(attachment_id, None)
            return len(stale)

    # ---------------------------------------------------------------- 重置令牌
    async def create_reset_token(self, user_id: str, token_hash: str, ttl_minutes: int) -> ResetToken:
        async with self._lock:
            token = ResetToken(
                token_hash=token_hash,
                user_id=user_id,
                expires_at=utcnow() + timedelta(minutes=ttl_minutes),
            )
            self.reset_tokens[token_hash] = token
            return token

    async def get_reset_token(self, token_hash: str) -> Optional[ResetToken]:
        return self.reset_tokens.get(token_hash)

    async def consume_reset_token(self, token_hash: str) -> Optional[ResetToken]:
        """取出并标记为已用（一次性）。"""
        async with self._lock:
            token = self.reset_tokens.get(token_hash)
            if token is None or token.used_at is not None:
                return None
            if token.expires_at < utcnow():
                return None
            token.used_at = utcnow()
            return token

    async def invalidate_reset_tokens(self, user_id: str) -> None:
        """用户重置密码后，作废其所有未使用的令牌。"""
        async with self._lock:
            for token in self.reset_tokens.values():
                if token.user_id == user_id and token.used_at is None:
                    token.used_at = utcnow()

    # ---------------------------------------------------------------- 系统配置
    async def list_configs(self) -> list[ConfigItem]:
        return list(self.configs.values())

    async def get_config(self, key: str) -> Optional[ConfigItem]:
        return self.configs.get(key)

    async def set_config(
        self,
        key: str,
        value: str,
        *,
        encrypted: bool = False,
        updated_by: Optional[str] = None,
    ) -> ConfigItem:
        async with self._lock:
            item = ConfigItem(
                key=key,
                value=value,
                encrypted=encrypted,
                updated_by=updated_by,
            )
            self.configs[key] = item
            return item

    async def delete_config(self, key: str) -> None:
        async with self._lock:
            self.configs.pop(key, None)

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
        async with self._lock:
            document = KnowledgeDocument(
                id=new_id(),
                user_id=user_id,
                filename=filename,
                file_type=file_type,
                size_bytes=size_bytes,
                status=status,
            )
            self.documents[document.id] = document
            return document

    async def get_document(self, document_id: str) -> Optional[KnowledgeDocument]:
        document = self.documents.get(document_id)
        if document and document.deleted_at is None:
            return document
        return None

    async def list_documents(self, user_id: str) -> list[KnowledgeDocument]:
        items = [
            d
            for d in self.documents.values()
            if d.user_id == user_id and d.deleted_at is None
        ]
        return sorted(items, key=lambda d: d.created_at, reverse=True)

    async def update_document(
        self, document: KnowledgeDocument, **fields: Any
    ) -> KnowledgeDocument:
        async with self._lock:
            for key, value in fields.items():
                if value is not None and hasattr(document, key):
                    setattr(document, key, value)
            document.updated_at = utcnow()
            return document

    async def delete_document(self, document: KnowledgeDocument) -> None:
        async with self._lock:
            document.deleted_at = utcnow()
            for chunk_id in [
                cid for cid, chunk in self.chunks.items() if chunk.document_id == document.id
            ]:
                self.chunks.pop(chunk_id, None)

    async def add_chunks(
        self,
        *,
        user_id: str,
        document_id: str,
        contents: list[str],
        source: str = "knowledge",
        embeddings: Optional[list[list[float]]] = None,
    ) -> int:
        async with self._lock:
            # max(seq)+1 而非 count：删除部分片段后 count 会与已有 seq 撞号
            seq = (
                max(
                    (
                        chunk.seq
                        for chunk in self.chunks.values()
                        if chunk.document_id == document_id
                    ),
                    default=-1,
                )
                + 1
            )
            created = 0
            for index, content in enumerate(contents):
                vector = (
                    embeddings[index]
                    if embeddings is not None and index < len(embeddings)
                    else None
                )
                chunk = EmbeddingChunk(
                    id=new_id(),
                    user_id=user_id,
                    content=content,
                    source=source,
                    document_id=document_id,
                    seq=seq,
                    embedding=list(vector) if vector else None,
                    meta={"index": seq},
                )
                self.chunks[chunk.id] = chunk
                seq += 1
                created += 1
            return created

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
        items = [chunk for chunk in self.chunks.values() if chunk.user_id == user_id]
        if document_id is not None:
            items = [chunk for chunk in items if chunk.document_id == document_id]
        if source is not None:
            items = [chunk for chunk in items if chunk.source == source]
        if only_embedded:
            items = [chunk for chunk in items if chunk.embedding]
        items.sort(key=lambda chunk: (chunk.document_id or "", chunk.seq))
        return items[offset : offset + limit]

    async def count_chunks(
        self,
        user_id: str,
        *,
        source: Optional[str] = None,
        only_embedded: bool = False,
    ) -> int:
        return sum(
            1
            for chunk in self.chunks.values()
            if chunk.user_id == user_id
            and (source is None or chunk.source == source)
            and (not only_embedded or bool(chunk.embedding))
        )

    async def update_chunk_embeddings(
        self, *, user_id: str, items: list[tuple[str, list[float]]]
    ) -> int:
        async with self._lock:
            updated = 0
            for chunk_id, vector in items:
                chunk = self.chunks.get(chunk_id)
                if chunk is None or chunk.user_id != user_id or not vector:
                    continue
                chunk.embedding = list(vector)
                updated += 1
            return updated

    async def delete_chunks(self, *, user_id: str, document_id: str) -> int:
        async with self._lock:
            targets = [
                cid
                for cid, chunk in self.chunks.items()
                if chunk.user_id == user_id and chunk.document_id == document_id
            ]
            for cid in targets:
                self.chunks.pop(cid, None)
            return len(targets)

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
        candidates = [
            chunk
            for chunk in self.chunks.values()
            if chunk.user_id == user_id
            and chunk.source == source
            and chunk.embedding
            and len(chunk.embedding) == len(vector)
        ][: max(1, candidate_limit)]
        if not candidates:
            return []
        scores = score_all(vector, [chunk.embedding or [] for chunk in candidates])
        matches: list[ChunkMatch] = []
        for chunk, score in sorted(
            zip(candidates, scores), key=lambda item: item[1], reverse=True
        ):
            if score < min_score:
                break
            document = self.documents.get(chunk.document_id or "")
            filename = (
                document.filename
                if document is not None and document.deleted_at is None
                else "未知文档"
            )
            matches.append(ChunkMatch(chunk=chunk, score=score, filename=filename))
            if len(matches) >= max(1, top_k):
                break
        return matches

    async def list_embedded_document_ids(
        self, user_id: str, *, source: str = "knowledge"
    ) -> set[str]:
        return {
            chunk.document_id
            for chunk in self.chunks.values()
            if chunk.user_id == user_id
            and chunk.source == source
            and chunk.embedding
            and chunk.document_id
        }

    # ---------------------------------------------------------------- 审计（M5）
    async def add_audit_log(self, entry: AuditLog) -> AuditLog:
        async with self._lock:
            self.audit_logs[entry.id] = entry
            return entry

    def _filter_audit_logs(
        self,
        user_id: Optional[str],
        *,
        session_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[AuditLog]:
        items = [
            log
            for log in self.audit_logs.values()
            if user_id is None or log.user_id == user_id
        ]
        if session_id:
            items = [log for log in items if log.session_id == session_id]
        if tool_name:
            items = [log for log in items if log.tool_name == tool_name]
        if status:
            items = [log for log in items if log.status == status]
        return sorted(items, key=lambda log: log.created_at, reverse=True)

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
        items = self._filter_audit_logs(
            user_id, session_id=session_id, tool_name=tool_name, status=status
        )
        return items[offset : offset + limit]

    async def count_audit_logs(
        self,
        user_id: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        return len(
            self._filter_audit_logs(
                user_id, session_id=session_id, tool_name=tool_name, status=status
            )
        )
