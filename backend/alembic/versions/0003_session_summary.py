"""会话摘要进度：sessions.summary_upto_seq

Revision ID: 0003_session_summary
Revises: 0002_system_config
Create Date: 2026-09-19

M4（FR-6）上下文压缩所需：

- `sessions.summary` 存放较早轮次的摘要文本；
- `summary_upto_seq` 记录摘要已覆盖到的 `messages.seq`，使压缩可以增量进行，
  避免每轮都把同一段历史重复喂给模型。默认 `-1` 表示尚未压缩过。

详见 `app/core/summary.py`。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_session_summary"
down_revision: Union[str, None] = "0002_system_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "summary_upto_seq",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("-1"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "summary_upto_seq")
