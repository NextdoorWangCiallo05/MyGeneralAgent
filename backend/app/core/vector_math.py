"""向量打分工具：MySQL / SQLite 等「无原生向量类型」的方言在应用层使用。

PostgreSQL + pgvector 的检索在数据库内完成（`<=>` 运算符），不经过这里；
保留本模块是为了让两套实现共享同一份余弦口径（numpy 可选，缺失时回退纯 Python）。
"""

from __future__ import annotations

import logging
from typing import Sequence

from app.core.embedding import cosine_similarity

logger = logging.getLogger("app.vector")

try:  # numpy 为可选依赖（local 嵌入模式下必然存在）；缺失时回退纯 Python
    import numpy as _np
except ImportError:  # pragma: no cover - 取决于运行环境
    _np = None


def score_all(query_vector: Sequence[float], vectors: Sequence[Sequence[float]]) -> list[float]:
    """批量计算余弦相似度（逐条与 `query_vector` 比较）。"""
    rows = list(vectors)
    if not rows:
        return []
    if _np is not None:
        try:
            matrix = _np.asarray(rows, dtype=_np.float32)
            query = _np.asarray(query_vector, dtype=_np.float32)
            denominator = _np.linalg.norm(matrix, axis=1) * float(_np.linalg.norm(query))
            denominator[denominator == 0] = 1.0
            return [float(score) for score in (matrix @ query) / denominator]
        except Exception:  # noqa: BLE001 - numpy 异常时回退纯 Python
            logger.warning("numpy 打分失败，回退纯 Python 实现", exc_info=True)
    return [cosine_similarity(query_vector, vector) for vector in rows]


__all__ = ["score_all"]
