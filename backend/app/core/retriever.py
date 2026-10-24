"""知识库检索（M4）：查询向量化 → 存储层向量检索 → top-k 片段。

检索已下推到存储层（`AbstractStore.search_chunks`）：

| 部署 | 实现 | 复杂度 |
|------|------|--------|
| PostgreSQL + pgvector | `ORDER BY embedding <=> :q LIMIT k`（HNSW 索引） | 与库大小近似无关 |
| MySQL / SQLite | 取前 `knowledge_search_max_chunks` 条后在应用层打分 | O(候选数) |

这样换存储不会影响上层：API、`knowledge_search` 工具、Agent 都只调用本模块。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.core.embedding import Embedder, get_embedder
from app.store.base import AbstractStore

logger = logging.getLogger("app.retriever")


@dataclass(slots=True)
class SearchHit:
    """一条检索命中。"""

    document_id: str | None
    filename: str
    seq: int
    score: float
    content: str


async def search_knowledge(
    store: AbstractStore,
    user_id: str,
    query: str,
    *,
    top_k: int | None = None,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
) -> list[SearchHit]:
    """在用户自己的知识库中检索，返回按相似度降序的片段；不可用时返回空列表。"""
    current = settings or get_settings()
    text = (query or "").strip()
    if not text:
        return []

    active = embedder if embedder is not None else get_embedder(current)
    if active is None:
        return []

    try:
        vectors = await active.encode([text])
    except Exception:  # noqa: BLE001 - 向量化失败不应中断对话
        logger.exception("查询向量化失败：%s", text[:60])
        return []
    if not vectors or not vectors[0]:
        return []
    query_vector = vectors[0]

    limit = max(1, min(int(top_k or current.knowledge_search_top_k), current.knowledge_search_top_k_max))
    try:
        matches = await store.search_chunks(
            user_id,
            query_vector,
            source="knowledge",
            top_k=limit,
            min_score=current.knowledge_search_min_score,
            candidate_limit=current.knowledge_search_max_chunks,
        )
    except Exception:  # noqa: BLE001 - 检索失败降级为「无命中」，不阻断对话
        logger.exception("知识库检索失败：%s", text[:60])
        return []

    return [
        SearchHit(
            document_id=match.chunk.document_id,
            filename=match.filename,
            seq=match.chunk.seq,
            score=match.score,
            content=match.chunk.content,
        )
        for match in matches
    ]


__all__ = ["SearchHit", "search_knowledge"]
