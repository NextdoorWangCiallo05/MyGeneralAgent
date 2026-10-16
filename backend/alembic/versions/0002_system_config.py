"""系统配置表：system_config

Revision ID: 0002_system_config
Revises: 0001_initial
Create Date: 2026-09-19

对应《技术设计文档》5.2 节：管理员后台维护的业务配置。
`key` 直接作为主键；敏感项（API-Key 等）以密文存入 `value`，
由 `encrypted` 标记，加解密逻辑见 `app/core/crypto.py`。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_system_config"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_config",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("encrypted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("system_config")
