"""SQLAlchemy 异步引擎与会话工厂。

设计要点：
- 引擎**惰性创建**：只有 `STORE_BACKEND=sql` 时才会真正连接数据库，
  内存模式下 import 本模块不产生任何副作用；
- 统一 `expire_on_commit=False`，提交后对象属性仍可读取，
  便于仓储在提交后把 ORM 行转成领域实体；
- SQLite（测试）使用 `StaticPool`，保证同一内存库在多个连接间共享。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.config import get_settings

__all__ = ["Base", "dispose_engine", "get_engine", "get_sessionmaker", "init_models", "session_scope"]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""


def get_engine() -> AsyncEngine:
    """返回进程内单例引擎。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("STORE_BACKEND=sql 时必须配置 DATABASE_URL")

        options: dict[str, object] = {"echo": settings.db_echo, "future": True}
        if settings.database_url.startswith("sqlite"):
            # SQLite 内存库：连接间共享同一份数据
            options["poolclass"] = StaticPool
        else:
            options.update(
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_recycle=settings.db_pool_recycle,
                pool_pre_ping=True,
            )
        _engine = create_async_engine(settings.database_url, **options)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """返回进程内单例会话工厂。"""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """一个「获取会话 → 提交/回滚 → 关闭」的事务作用域。"""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """按 ORM 元数据建表（开发便捷入口；生产用 Alembic 迁移）。"""
    from app import models  # noqa: F401 - 触发模型注册

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    """释放连接池（应用关闭 / 测试清理）。"""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
