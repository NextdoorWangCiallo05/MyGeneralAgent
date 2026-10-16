"""知识库检索（M4）：查询向量化 → 余弦相似度 → top-k 片段。

MySQL 8 没有原生向量类型，`embeddings.embedding` 以 JSON 文本保存向量，
因此这里采用**应用层暴力打分**：取出该用户已向量化的全部文本块（上限见
`knowledge_search_max_chunks`），逐条算余弦相似度。几万条 chunk 的量级下
延迟仍在百毫秒级；数据量继续增长时可平滑替换为 pgvector / 独立向量库。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.core.embedding import Embedder, cosine_similarity, get_embedder
from app.store.base import AbstractStore

logger = logging.getLogger("app.retriever")

try:  # numpy 为可选依赖（local 嵌入模式下必然存在）；缺失时回退纯 Python
    import numpy as _np
except ImportError:  # pragma: no cover - 取决于运行环境
    _np = None


@dataclass(slots=True)
class SearchHit:
    """一条检索命中。"""

    document_id: str | None
    filename: str
    seq: int
    score: float
    content: str


def _score_all(query_vector: list[float], vectors: list[list[float]]) -> list[float]:
    """批量计算余弦相似度。"""
    if _np is not None and vectors:
        try:
            matrix = _np.asarray(vectors, dtype=_np.float32)
            query = _np.asarray(query_vector, dtype=_np.float32)
            denominator = _np.linalg.norm(matrix, axis=1) * float(_np.linalg.norm(query))
            denominator[denominator == 0] = 1.0
            return [float(score) for score in (matrix @ query) / denominator]
        except Exception:  # noqa: BLE001 - numpy 异常时回退纯 Python
            logger.warning("numpy 打分失败，回退纯 Python 实现", exc_info=True)
    return [cosine_similarity(query_vector, vector) for vector in vectors]


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
    chunks = await store.list_chunks(
        user_id,
        source="knowledge",
        only_embedded=True,
        limit=current.knowledge_search_max_chunks,
    )
    # 维度不一致说明是切换嵌入后端前遗留的旧向量，跳过（需重建索引）
    candidates = [
        chunk
        for chunk in chunks
        if chunk.embedding and len(chunk.embedding) == len(query_vector)
    ]
    if not candidates:
        return []

    scores = _score_all(query_vector, [chunk.embedding or [] for chunk in candidates])
    ranked = sorted(zip(candidates, scores), key=lambda item: item[1], reverse=True)

    filenames = {doc.id: doc.filename for doc in await store.list_documents(user_id)}
    hits: list[SearchHit] = []
    for chunk, score in ranked[:limit]:
        if score < current.knowledge_search_min_score:
            break
        hits.append(
            SearchHit(
                document_id=chunk.document_id,
                filename=filenames.get(chunk.document_id or "", "未知文档"),
                seq=chunk.seq,
                score=score,
                content=chunk.content,
            )
        )
    return hits


__all__ = ["SearchHit", "search_knowledge"]
