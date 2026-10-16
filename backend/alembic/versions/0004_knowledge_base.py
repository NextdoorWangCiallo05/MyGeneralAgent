"""知识库：knowledge_documents / embeddings

Revision ID: 0004_knowledge_base
Revises: 0003_session_summary
Create Date: 2026-09-19

M4（RAG 知识库）的存储层，对应《技术设计文档》5.2 节：

- `knowledge_documents`：用户上传的文档元信息（解析状态、分块数等）；
- `embeddings`：可检索文本块。原设计用 pgvector，实际落库为 MySQL 8，
  没有原生向量类型，因此 `embedding` 以 **JSON 文本**保存，
  检索在应用层计算余弦相似度（见 app/core/chunking.py 与 M4 检索实现）。

`source` 区分知识库文档（`knowledge`）与长期记忆（`memory`），
两类文本块共用一张表，便于后续统一检索。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_knowledge_base"
down_revision: Union[str, None] = "0003_session_summary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_type", sa.String(length=16), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ready"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_documents_user_id", "knowledge_documents", ["user_id"])

    op.create_table(
        "embeddings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="knowledge"),
        sa.Column("seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "seq", name="uq_embeddings_document_seq"),
    )
    op.create_index("ix_embeddings_user_id", "embeddings", ["user_id"])
    op.create_index("ix_embeddings_document_id", "embeddings", ["document_id"])
    op.create_index("ix_embeddings_user_source", "embeddings", ["user_id", "source"])


def downgrade() -> None:
    op.drop_index("ix_embeddings_user_source", table_name="embeddings")
    op.drop_index("ix_embeddings_document_id", table_name="embeddings")
    op.drop_index("ix_embeddings_user_id", table_name="embeddings")
    op.drop_table("embeddings")

    op.drop_index("ix_knowledge_documents_user_id", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
