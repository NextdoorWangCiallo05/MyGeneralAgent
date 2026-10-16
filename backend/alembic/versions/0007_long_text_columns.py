"""大文本列改为 LONGTEXT：attachments.text / messages.content / embeddings.embedding

Revision ID: 0007_long_text_columns
Revises: 0006_chat_attachments
Create Date: 2026-10-16

MySQL 的 `TEXT` 上限是 **65535 字节**（不是字符），而这几列都会超：

- `attachments.text`：上传 1 MB 的 PDF 就能抽出 4.9 万汉字 ≈ 147 KB，
  插入时报 `DataError (1406) Data too long for column 'text'`，用户侧表现为「上传失败」；
- `messages.content`：附件正文会拼进本轮用户消息，单条消息最多 5 个附件、
  每个注入上限 2 万字符，10 万汉字 ≈ 300 KB；
- `embeddings.embedding`：3072 维向量序列化成 JSON 约 60 KB，已贴近上限。

统一改为 `LONGTEXT`（上限 4 GB）。SQLite 等方言没有该限制，故本迁移只对 MySQL 生效
（OR/M 侧由 `app.models.LONG_TEXT` 的 `with_variant` 保持一致）。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0007_long_text_columns"
down_revision: Union[str, None] = "0006_chat_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (表, 列, 是否可空)，需要从 TEXT 放宽到 LONGTEXT 的大文本列
COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("attachments", "text", False),
    ("messages", "content", False),
    ("embeddings", "embedding", True),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    for table, column, nullable in COLUMNS:
        # 只改类型：nullability 按原样带上，MySQL 下是原地 ALTER
        op.alter_column(
            table,
            column,
            existing_type=sa.Text(),
            type_=mysql.LONGTEXT(),
            existing_nullable=nullable,
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    for table, column, nullable in COLUMNS:
        # 注意：回退到 TEXT 时，超过 65535 字节的存量数据会被截断（严格模式下直接报错）
        op.alter_column(
            table,
            column,
            existing_type=mysql.LONGTEXT(),
            type_=sa.Text(),
            existing_nullable=nullable,
        )
