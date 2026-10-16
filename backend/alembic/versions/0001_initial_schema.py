"""初始表结构：users / sessions / messages / password_reset_tokens

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-19

对应《技术设计文档》5.2 节（M2 范围）：用户、会话、消息、密码重置令牌。
主键统一为 String(36) 存放 UUID 字符串；时间列为 naive UTC DATETIME。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("sandbox_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------------ sessions
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False, server_default="新会话"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("sandbox_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    # ------------------------------------------------------------ messages
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_session_seq", "messages", ["session_id", "seq"])

    # ------------------------------------------------------------ password_reset_tokens
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
    )
    op.create_index(
        "ix_password_reset_tokens_token_hash",
        "password_reset_tokens",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_password_reset_tokens_token_hash", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")

    op.drop_index("ix_messages_session_seq", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
