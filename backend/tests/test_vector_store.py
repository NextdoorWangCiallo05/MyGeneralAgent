"""向量检索下推测试：存储层 top-k、pgvector 列类型、检索降级。

不依赖 PostgreSQL 的部分（内存 / SQLite / DDL 编译）在这里全部覆盖；
真实 pgvector 的 SQL 由 `test_pgvector_integration.py` 在配置了
`PGVECTOR_TEST_URL` 时执行。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite

from app.config import get_settings
from app.core.embedding import HashEmbedder
from app.core.retriever import search_knowledge
from app.db import dispose_engine, init_models
from app.models import EmbeddingChunkRow
from app.models.vector import EmbeddingVector, vector_literal
from app.store import AbstractStore, get_store, reset_store
from app.store.memory import MemoryStore


def _run(coro) -> object:
    return asyncio.run(coro)


async def _seed(store: AbstractStore, user_id: str, contents: list[str], vectors: list[list[float]]):
    document = await store.create_document(user_id=user_id, filename="guide.md", file_type="md")
    await store.add_chunks(
        user_id=user_id,
        document_id=document.id,
        contents=contents,
        embeddings=vectors,
    )
    return document


# ---------------------------------------------------------------- 内存实现
def test_memory_search_ranks_filters_and_scopes() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        document = await _seed(
            store,
            "u1",
            ["a", "b", "c"],
            [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]],
        )
        # 其它用户与其它来源的片段不得混入
        await _seed(store, "u2", ["别人的"], [[1.0, 0.0]])
        await store.add_chunks(
            user_id="u1",
            document_id=document.id,
            contents=["记忆片段"],
            source="memory",
            embeddings=[[1.0, 0.0]],
        )

        matches = await store.search_chunks("u1", [1.0, 0.0], top_k=2, min_score=0.5)

        assert [match.chunk.content for match in matches] == ["a", "b"]
        assert matches[0].score == pytest.approx(1.0)
        assert matches[1].score > 0.9
        assert all(match.filename == "guide.md" for match in matches)
        assert all(match.chunk.source == "knowledge" for match in matches)

    _run(scenario())


def test_memory_search_skips_dimension_mismatch() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        await _seed(store, "u1", ["旧维度"], [[1.0, 0.0, 0.0]])

        assert await store.search_chunks("u1", [1.0, 0.0]) == []
        assert await store.search_chunks("u1", []) == []

    _run(scenario())


def test_memory_concurrent_add_chunks_keeps_seq_unique() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        document = await store.create_document(user_id="u1", filename="guide.md", file_type="md")

        await asyncio.gather(
            *[
                store.add_chunks(
                    user_id="u1",
                    document_id=document.id,
                    contents=[f"chunk-{index}"],
                )
                for index in range(20)
            ]
        )

        chunks = await store.list_chunks("u1", limit=100)
        assert sorted(chunk.seq for chunk in chunks) == list(range(20))

    _run(scenario())


def test_memory_embedded_document_ids() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        indexed = await _seed(store, "u1", ["已索引"], [[1.0, 0.0]])
        await store.create_document(user_id="u1", filename="empty.md", file_type="md")

        assert await store.list_embedded_document_ids("u1") == {indexed.id}
        assert await store.list_embedded_document_ids("u2") == set()

    _run(scenario())


# ---------------------------------------------------------------- 检索装配
def test_search_knowledge_end_to_end_with_hash_embedder() -> None:
    async def scenario() -> None:
        embedder = HashEmbedder(dim=128)
        store = MemoryStore()
        texts = ["pgvector 支持余弦距离检索", "今天天气不错适合散步"]
        await _seed(store, "u1", texts, await embedder.encode(texts))

        hits = await search_knowledge(
            store,
            "u1",
            "pgvector 余弦检索",
            embedder=embedder,
            top_k=1,
        )

        assert len(hits) == 1
        assert hits[0].filename == "guide.md"
        assert hits[0].seq == 0
        assert "pgvector" in hits[0].content
        assert hits[0].score > 0.1

    _run(scenario())


def test_search_knowledge_degrades_when_store_fails() -> None:
    class BrokenStore:
        async def search_chunks(self, *args, **kwargs):
            raise RuntimeError("数据库连接中断")

    async def scenario() -> None:
        hits = await search_knowledge(BrokenStore(), "u1", "任意查询")  # type: ignore[arg-type]
        assert hits == []

    _run(scenario())


# ---------------------------------------------------------------- 列类型
def test_embedding_column_type_per_dialect() -> None:
    column_type = EmbeddingChunkRow.__table__.c.embedding.type

    assert "LONGTEXT" in str(column_type.compile(dialect=mysql.dialect())).upper()
    assert "TEXT" in str(column_type.compile(dialect=sqlite.dialect())).upper()

    pytest.importorskip("pgvector")
    ddl = str(column_type.compile(dialect=postgresql.dialect())).strip().upper()
    assert ddl == f"VECTOR({get_settings().embedding_dim})"


def test_vector_literal_and_json_round_trip() -> None:
    literal = vector_literal([0.1, -2.0, 3])
    assert literal == "[0.1,-2.0,3.0]"
    assert json.loads(literal) == [0.1, -2.0, 3.0]

    column_type = EmbeddingVector(3)
    bound = column_type.process_bind_param([1, 2.5], sqlite.dialect())
    assert bound == "[1.0,2.5]"
    assert column_type.process_result_value(bound, sqlite.dialect()) == [1.0, 2.5]
    assert column_type.process_result_value(None, sqlite.dialect()) is None


# ---------------------------------------------------------------- SQLite 实现
@pytest.fixture
def sql_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("STORE_BACKEND", "sql")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    reset_store()
    yield
    reset_store()
    get_settings.cache_clear()


def test_sqlite_search_falls_back_to_application_scoring(sql_env: None) -> None:
    async def scenario() -> None:
        await dispose_engine()
        reset_store()
        await init_models()
        store = get_store()
        assert getattr(store, "supports_native_vectors", False) is False

        document = await _seed(
            store,
            "u1",
            ["a", "b", "c"],
            [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]],
        )
        await _seed(store, "u2", ["别人的"], [[1.0, 0.0]])

        matches = await store.search_chunks("u1", [1.0, 0.0], top_k=2, min_score=0.5)

        assert [match.chunk.content for match in matches] == ["a", "b"]
        assert all(match.filename == "guide.md" for match in matches)
        assert await store.list_embedded_document_ids("u1") == {document.id}
        await dispose_engine()

    _run(scenario())
