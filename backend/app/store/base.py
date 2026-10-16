"""仓储抽象接口：定义上层可见的全部数据访问契约。

`MemoryStore`（进程内）与 `SqlStore`（MySQL/SQLite）必须实现同一组方法，
调用方只依赖本接口，切换后端无需改动业务代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from app.store.entities import (
    Attachment,
    AuditLog,
    ConfigItem,
    EmbeddingChunk,
    KnowledgeDocument,
    Message,
    ResetToken,
    Session,
    User,
)


class AbstractStore(ABC):
    """持久化仓储统一契约（全部方法均为协程）。"""

    # ---------------------------------------------------------------- 用户
    @abstractmethod
    async def create_user(
        self,
        *,
        username: str,
        email: str,
        password_hash: str,
        role: str = "user",
    ) -> User: ...

    @abstractmethod
    async def get_user(self, user_id: str) -> Optional[User]: ...

    @abstractmethod
    async def get_user_by_username(self, username: str) -> Optional[User]: ...

    @abstractmethod
    async def get_user_by_email(self, email: str) -> Optional[User]: ...

    @abstractmethod
    async def get_user_by_login(self, identifier: str) -> Optional[User]: ...

    @abstractmethod
    async def list_users(self) -> list[User]: ...

    @abstractmethod
    async def update_password(self, user_id: str, password_hash: str) -> None: ...

    # ---------------------------------------------------------------- 会话
    @abstractmethod
    async def create_session(
        self,
        *,
        user_id: str,
        title: str = "新会话",
        model: str = "",
        sandbox_enabled: bool = False,
    ) -> Session: ...

    @abstractmethod
    async def get_session(self, session_id: str) -> Optional[Session]: ...

    @abstractmethod
    async def list_sessions(self, user_id: str) -> list[Session]: ...

    @abstractmethod
    async def update_session(self, session: Session, **fields: Any) -> Session: ...

    @abstractmethod
    async def delete_session(self, session: Session) -> None: ...

    # ---------------------------------------------------------------- 消息
    @abstractmethod
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
    ) -> Message: ...

    @abstractmethod
    async def list_messages(self, session_id: str) -> list[Message]: ...

    @abstractmethod
    async def delete_messages_from(self, session_id: str, seq: int) -> int:
        """删除 `seq` 及其之后的全部消息，返回删除条数。

        用于「重新生成」：把最后一轮（提问 + 回答 + 工具结果）整体摘掉再重跑，
        避免同一问题重复入历史导致模型看到上一版答案。
        """

    # ---------------------------------------------------------------- 对话附件
    @abstractmethod
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
    ) -> Attachment: ...

    @abstractmethod
    async def get_attachment(self, attachment_id: str) -> Optional[Attachment]: ...

    @abstractmethod
    async def list_attachments(self, attachment_ids: list[str]) -> list[Attachment]:
        """按 id 批量取出附件（不存在的 id 直接跳过，顺序与入参一致）。"""

    @abstractmethod
    async def list_session_attachments(self, session_id: str) -> list[Attachment]:
        """列出绑定在该会话上的全部附件（按上传时间正序）。

        附件是**会话级**资源：上传一次后，后续每一轮都要能重新注入上下文，
        因此这里不能只按当前消息取（见 `Agent._render_session_files`）。
        尚未随消息发出的附件 `session_id` 为空，不属于任何会话。
        """

    @abstractmethod
    async def bind_attachments(
        self,
        attachment_ids: list[str],
        *,
        message_id: str,
        session_id: str,
    ) -> int:
        """把附件绑定到刚落库的消息，返回实际更新条数。"""

    @abstractmethod
    async def delete_attachment(self, attachment: Attachment) -> None: ...

    @abstractmethod
    async def purge_stale_attachments(self, *, before: datetime) -> int:
        """清理 `before` 之前上传、且尚未随消息发出的附件，返回清理条数。"""

    # ---------------------------------------------------------------- 重置令牌
    @abstractmethod
    async def create_reset_token(
        self, user_id: str, token_hash: str, ttl_minutes: int
    ) -> ResetToken: ...

    @abstractmethod
    async def get_reset_token(self, token_hash: str) -> Optional[ResetToken]: ...

    @abstractmethod
    async def consume_reset_token(self, token_hash: str) -> Optional[ResetToken]: ...

    @abstractmethod
    async def invalidate_reset_tokens(self, user_id: str) -> None: ...

    # ---------------------------------------------------------------- 系统配置
    @abstractmethod
    async def list_configs(self) -> list[ConfigItem]: ...

    @abstractmethod
    async def get_config(self, key: str) -> Optional[ConfigItem]: ...

    @abstractmethod
    async def set_config(
        self,
        key: str,
        value: str,
        *,
        encrypted: bool = False,
        updated_by: Optional[str] = None,
    ) -> ConfigItem: ...

    @abstractmethod
    async def delete_config(self, key: str) -> None: ...

    # ---------------------------------------------------------------- 知识库
    @abstractmethod
    async def create_document(
        self,
        *,
        user_id: str,
        filename: str,
        file_type: str,
        size_bytes: int = 0,
        status: str = "ready",
    ) -> KnowledgeDocument: ...

    @abstractmethod
    async def get_document(self, document_id: str) -> Optional[KnowledgeDocument]: ...

    @abstractmethod
    async def list_documents(self, user_id: str) -> list[KnowledgeDocument]: ...

    @abstractmethod
    async def update_document(
        self, document: KnowledgeDocument, **fields: Any
    ) -> KnowledgeDocument: ...

    @abstractmethod
    async def delete_document(self, document: KnowledgeDocument) -> None:
        """软删文档，并清理其全部文本块。"""

    @abstractmethod
    async def add_chunks(
        self,
        *,
        user_id: str,
        document_id: str,
        contents: list[str],
        source: str = "knowledge",
        embeddings: Optional[list[list[float]]] = None,
    ) -> int:
        """批量写入文本块，返回写入条数（`seq` 在文档内连续递增）。

        `embeddings` 与 `contents` 按下标对齐；为空表示该批文本块尚未向量化。
        """

    @abstractmethod
    async def list_chunks(
        self,
        user_id: str,
        *,
        document_id: Optional[str] = None,
        source: Optional[str] = None,
        only_embedded: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EmbeddingChunk]: ...

    @abstractmethod
    async def count_chunks(
        self,
        user_id: str,
        *,
        source: Optional[str] = None,
        only_embedded: bool = False,
    ) -> int: ...

    @abstractmethod
    async def update_chunk_embeddings(
        self,
        *,
        user_id: str,
        items: list[tuple[str, list[float]]],
    ) -> int:
        """按文本块 id 回填向量，返回实际更新条数（不属于该用户的块会被忽略）。"""

    @abstractmethod
    async def delete_chunks(self, *, user_id: str, document_id: str) -> int: ...

    # ---------------------------------------------------------------- 审计（M5）
    @abstractmethod
    async def add_audit_log(self, entry: AuditLog) -> AuditLog:
        """写入一条工具调用审计记录。"""

    @abstractmethod
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
        """按时间倒序列出审计记录，供管理后台查询。

        `user_id` 为 None 时跨用户查询（仅管理后台使用），否则按用户隔离。
        """

    @abstractmethod
    async def count_audit_logs(
        self,
        user_id: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int: ...


__all__ = ["AbstractStore"]
