"""工具调用审计：tool_audit_logs

Revision ID: 0005_tool_audit_logs
Revises: 0004_knowledge_base
Create Date: 2026-09-19

M5（可观测性）的审计落库，对应《技术设计文档》5.2 节的 `tool_audit_logs`：

- 记录「谁、在哪个会话、让 Agent 调用了什么工具、参数、结果、耗时、是否经过确认」；
- 相比设计文档额外增加 `user_id`（免 join sessions 即可按用户过滤）
  与 `approved`（M3 人工确认结果）；
- `args` / `result` 存 JSON 文本；`args` 落库前已脱敏、`result` 只存摘要
  （见 app/core/audit.py），因此本表可安全长期保留。

查询以「会话 + 时间」为主（时间倒序翻页），故建联合索引
`ix_tool_audit_session_created`；`user_id` 另建单列索引支撑按用户过滤。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_tool_audit_logs"
down_revision: Union[str, None] = "0004_knowledge_base"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("args", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="success"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_audit_logs_user_id", "tool_audit_logs", ["user_id"])
    op.create_index(
        "ix_tool_audit_session_created",
        "tool_audit_logs",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_audit_session_created", table_name="tool_audit_logs")
    op.drop_index("ix_tool_audit_logs_user_id", table_name="tool_audit_logs")
    op.drop_table("tool_audit_logs")
