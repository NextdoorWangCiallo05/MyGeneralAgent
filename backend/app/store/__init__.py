"""存储层入口：按 `STORE_BACKEND` 配置返回内存或 SQL 仓储。"""

from __future__ import annotations

from app.config import get_settings
from app.store.base import AbstractStore
from app.store.entities import (
    Attachment,
    AuditLog,
    ConfigItem,
    Message,
    ResetToken,
    Session,
    User,
)
from app.store.memory import MemoryStore

__all__ = [
    "AbstractStore",
    "Attachment",
    "AuditLog",
    "ConfigItem",
    "MemoryStore",
    "Message",
    "ResetToken",
    "Session",
    "User",
    "get_store",
    "reset_store",
]

_store: AbstractStore | None = None


def get_store() -> AbstractStore:
    """进程内单例仓储（延迟创建，避免无谓的数据库连接）。"""
    global _store
    if _store is None:
        settings = get_settings()
        if settings.store_backend.strip().lower() == "sql":
            # 延迟导入：内存模式下不会加载数据库驱动
            from app.db.base import get_sessionmaker
            from app.store.sql import SqlStore

            _store = SqlStore(get_sessionmaker())
        else:
            _store = MemoryStore()
    return _store


def reset_store() -> None:
    """清空单例（测试或运行时切换后端时使用）。"""
    global _store
    _store = None
