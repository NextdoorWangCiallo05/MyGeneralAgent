"""pgvector 集成测试（默认跳过）。

需要一个**可清空的** PostgreSQL 库，且已安装 pgvector 扩展：

    set PGVECTOR_TEST_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/agent_test
    .venv/Scripts/python.exe -m pytest tests/test_pgvector_integration.py -q

测试会删除该库里的 `embeddings` / `knowledge_documents` 两张表后重建，
务必指向专用测试库，不要指向开发或生产库。
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base
from app.store.sql import SqlStore

URL = os.getenv("PGVECTOR_TEST_URL")

pytestmark = pytest.mark.skipif(
    not URL, reason="未设置 PGVECTOR_TEST_URL，跳过真实 pgvector 集成测试"
)


def _basis(dim: int, index: int) -> list[float]:
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


def test_pgvector_native_search() -> None:
    async def scenario() -> None:
        dim = int(get_settings().embedding_dim)
        engine = create_async_engine(URL)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.execute(text("DROP TABLE IF EXISTS embeddings"))
                await conn.execute(text("DROP TABLE IF EXISTS knowledge_documents"))
                await conn.run_sync(Base.metadata.create_all)

            store = SqlStore(async_sessionmaker(bind=engine, expire_on_commit=False))
            assert store.supports_native_vectors is True

            user = await store.create_user(
                username="pgvector", email="pgvector@example.com", password_hash="h"
            )
            document = await store.create_document(
                user_id=user.id, filename="guide.md", file_type="md"
            )
            await store.add_chunks(
                user_id=user.id,
                document_id=document.id,
                contents=["alpha", "beta"],
                embeddings=[_basis(dim, 0), _basis(dim, 1)],
            )

            matches = await store.search_chunks(user.id, _basis(dim, 0), top_k=1)
            assert [match.chunk.content for match in matches] == ["alpha"]
            assert matches[0].score == pytest.approx(1.0, abs=1e-5)
            assert matches[0].filename == "guide.md"
            assert await store.list_embedded_document_ids(user.id) == {document.id}
        finally:
            await engine.dispose()

    asyncio.run(scenario())
