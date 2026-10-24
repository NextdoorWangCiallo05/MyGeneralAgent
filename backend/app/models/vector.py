"""向量列类型：PostgreSQL 用 pgvector，其余方言保持 JSON 文本。

为什么需要它：`embeddings.embedding` 在 MySQL 上是 `LONGTEXT`（JSON 文本），
到 PostgreSQL 上则应该是原生 `vector(N)`，才能用 `<=>` 运算符与 HNSW 索引做
「数据库内 top-k」。同一套 ORM 模型要同时服务两种方言，因此用 `TypeDecorator`
按方言切换底层类型：

| 方言 | 物理类型 | 读写形态 |
|------|----------|----------|
| PostgreSQL | `vector(dim)` | Python `list[float]`（由 pgvector 驱动编解码） |
| MySQL | `LONGTEXT` | JSON 文本 |
| SQLite | `TEXT` | JSON 文本 |

维度取自 `EMBEDDING_DIM`：pgvector 的索引要求定长列，因此**同一部署内不要
随意改维度**；切换嵌入模型（如 512 → 1024 维）需要重建列并 `force` 重建索引，
见 `alembic/versions/0008_pgvector_embeddings.py` 与 README「pgvector」。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Text
from sqlalchemy.dialects import mysql
from sqlalchemy.types import TypeDecorator, TypeEngine

from app.config import get_settings


class EmbeddingVector(TypeDecorator[list[float]]):
    """按方言落地的向量列（PostgreSQL 原生 vector，其余 JSON 文本）。"""

    impl = Text
    cache_ok = True

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dim = max(1, int(dim))

    def load_dialect_impl(self, dialect: Any) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector
            except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可操作提示
                raise RuntimeError(
                    "PostgreSQL 向量检索需要 pgvector：pip install 'pgvector>=0.3'"
                ) from exc
            return dialect.type_descriptor(Vector(self.dim))
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.LONGTEXT())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        vector = [float(item) for item in value]
        if dialect.name == "postgresql":
            # 交给 pgvector 的绑定处理器（asyncpg 需要 register_vector）
            return vector
        # 紧凑分隔符：3072 维向量能少存约 6k 字符
        return json.dumps(vector, ensure_ascii=False, separators=(",", ":"))

    def process_result_value(self, value: Any, dialect: Any) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, (str, bytes)):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return None
            value = parsed
        # pgvector 返回 Vector 对象、numpy 返回 ndarray，两者都需要先转成 list
        if hasattr(value, "to_list"):
            value = value.to_list()
        elif hasattr(value, "tolist"):
            value = value.tolist()
        try:
            return [float(item) for item in value]
        except (TypeError, ValueError):
            return None


def embedding_dim() -> int:
    """当前配置的向量维度（模型列定义与迁移共用）。"""
    return max(1, int(get_settings().embedding_dim))


def vector_literal(vector: Sequence[float]) -> str:
    """把向量转成 pgvector 的文本字面量：`[0.1,0.2]`。

    原生 SQL（`text()`）不经过 ORM 的类型绑定处理器，因此这里显式给字符串，
    再在 SQL 里 `CAST(CAST(:q AS text) AS vector)`（先固定参数类型为 text，
    避免驱动去推断 vector 类型而要求注册 codec）。
    """
    return "[" + ",".join(repr(float(item)) for item in vector) + "]"


__all__ = ["EmbeddingVector", "embedding_dim", "vector_literal"]
