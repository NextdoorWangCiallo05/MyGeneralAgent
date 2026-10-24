"""PostgreSQL 向量列改为 pgvector，并建立 HNSW 索引

Revision ID: 0008_pgvector_embeddings
Revises: 0007_long_text_columns
Create Date: 2026-10-24

只对 PostgreSQL 生效，MySQL / SQLite 仍是 JSON 文本（应用层余弦）。

`USING NULL` 的原因：旧数据是 JSON 文本，维度可能与 `EMBEDDING_DIM` 不一致
（例如从 1024 维模型切到 512 维）。pgvector 是定长列，逐行强转会因个别脏数据
导致整个迁移失败，因此这里**清空旧向量**，升级后到知识库点一次「重建索引」
（`POST /api/v1/knowledge/reindex {"force": true}`）即可。

索引优先 HNSW（pgvector ≥ 0.5），服务端版本过旧时自动回退 ivfflat。
首次建索引时表通常为空，耗时可忽略；若已有大量数据，建议在业务低峰执行。

前置条件：
1. 数据库已安装 pgvector 扩展（`CREATE EXTENSION vector` 需要超级用户或已预装）；
2. 应用侧安装 `pgvector` Python 包（requirements.txt 已包含）：ORM 读写走它的
   `Vector` 绑定/解析器，原生检索 SQL 则用 `vector_literal()` 显式传文本再 CAST。
"""

from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op

revision: str = "0008_pgvector_embeddings"
down_revision: Union[str, None] = "0007_long_text_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

HNSW_INDEX = "ix_embeddings_embedding_hnsw"
IVFFLAT_INDEX = "ix_embeddings_embedding_ivfflat"


def _embedding_dim() -> int:
    raw = os.getenv("EMBEDDING_DIM")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    from app.config import get_settings

    return max(1, int(get_settings().embedding_dim))


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    dim = _embedding_dim()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # 旧向量（JSON 文本）在这里统一置空；升级后需要重建索引
    op.execute(f"ALTER TABLE embeddings ALTER COLUMN embedding TYPE vector({dim}) USING NULL")
    op.execute(
        f"""
        DO $$
        BEGIN
            BEGIN
                CREATE INDEX IF NOT EXISTS {HNSW_INDEX}
                    ON embeddings USING hnsw (embedding vector_cosine_ops);
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'HNSW 索引不可用（pgvector 版本过旧？），回退 ivfflat';
                CREATE INDEX IF NOT EXISTS {IVFFLAT_INDEX}
                    ON embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
            END;
        END $$
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {HNSW_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {IVFFLAT_INDEX}")
    # vector::text 输出形如 "[1,2]"，仍是合法 JSON，旧版应用可继续按文本读取
    op.execute("ALTER TABLE embeddings ALTER COLUMN embedding TYPE TEXT USING embedding::text")
