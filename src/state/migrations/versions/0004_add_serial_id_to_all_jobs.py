"""Add BIGSERIAL id column to all_jobs for deterministic insertion order.

Revision ID: 0004_add_serial_id
Revises: 0003_pivot_schema
Create Date: 2026-06-11

Adds ``id BIGSERIAL`` to ``all_jobs`` so rows have a stable, monotonically
increasing insertion-order key. Existing rows are backfilled by the sequence
in heap order; new rows get incrementing ids automatically.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004_add_serial_id"
down_revision: Union[str, None] = "0003_pivot_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE all_jobs ADD COLUMN id BIGSERIAL")
    op.create_index("idx_all_jobs_insert_order", "all_jobs", ["id"])


def downgrade() -> None:
    op.drop_index("idx_all_jobs_insert_order", table_name="all_jobs")
    op.drop_column("all_jobs", "id")
