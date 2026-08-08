"""Add user_status_at to applied, for ordering the operator's Applied list.

Revision ID: 0005_user_status_at
Revises: 0004_add_serial_id
Create Date: 2026-08-08

``applied.user_status`` already tracked pending | applied | skipped, but
nothing recorded WHEN it changed, so the dashboard could not answer "what did
I apply to, most recent first" — it could only order by ``built_at``, which is
when the resume was built, not when the operator acted.

Backfill is deliberately NULL rather than ``built_at``: pretending we know the
action date would put wrong dates on the resume record. Rows already marked
applied sort last until they are touched again.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_user_status_at"
down_revision: Union[str, None] = "0004_add_serial_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "applied",
        sa.Column("user_status_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The dashboard's Applied and Matches views both filter on status.
    op.create_index("idx_applied_user_status", "applied", ["user_status"])


def downgrade() -> None:
    op.drop_index("idx_applied_user_status", table_name="applied")
    op.drop_column("applied", "user_status_at")
