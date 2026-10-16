"""对话附件：attachments 表 + messages.attachments

Revision ID: 0006_chat_attachments
Revises: 0005_tool_audit_logs
Create Date: 2026-10-16

「对话附件」功能：用户在聊天输入框选文件随消息发送。表结构说明：

- `attachments.text` 存解析后的纯文本，发送时注入模型上下文；
- `message_id` 为空表示「已上传但还没随消息发出」，超过 TTL 由
  `purge_stale_attachments` 清理；
- `messages.attachments` 只存元数据快照（id / 文件名 / 大小），
  正文留在 attachments 表，避免历史消息把大段文本重复塞进 messages。

查询以「用户 + 消息」为主，故建联合索引 `ix_attachments_user_message`；
`user_id` / `message_id` 另建单列索引支撑各自过滤。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_chat_attachments"
down_revision: Union[str, None] = "0005_tool_audit_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_type", sa.String(length=16), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attachments_user_id", "attachments", ["user_id"])
    op.create_index("ix_attachments_message_id", "attachments", ["message_id"])
    op.create_index("ix_attachments_user_message", "attachments", ["user_id", "message_id"])
    op.add_column("messages", sa.Column("attachments", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "attachments")
    op.drop_index("ix_attachments_user_message", table_name="attachments")
    op.drop_index("ix_attachments_message_id", table_name="attachments")
    op.drop_index("ix_attachments_user_id", table_name="attachments")
    op.drop_table("attachments")
