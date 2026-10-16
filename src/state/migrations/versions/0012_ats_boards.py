"""ATS job boards: every board id tried and every board found (#97).

``ats_probes`` holds each (ats, board_id) ever probed, misses included, so a
rerun or a new id source never asks twice. ``ats_boards`` holds every board
that exists, with or without India jobs, so later polling and analysis can
filter by location instead of losing the rest. ``poll`` marks the boards the
hourly poller fetches; the counts beside it come from the last measurement.

Revision ID: 0012_ats_boards
Revises: 0011_portal_fields
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012_ats_boards"
down_revision: Union[str, None] = "0011_portal_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ats_probes",
        sa.Column("ats", sa.Text(), primary_key=True),
        sa.Column("board_id", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("hit", sa.Boolean(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ats_probes_pending", "ats_probes", ["ats", "hit"])
    op.create_table(
        "ats_boards",
        sa.Column("ats", sa.Text(), primary_key=True),
        sa.Column("board_id", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("board_name", sa.Text(), nullable=True),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("job_count", sa.Integer(), nullable=True),
        sa.Column("india_job_count", sa.Integer(), nullable=True),
        sa.Column("remote_job_count", sa.Integer(), nullable=True),
        sa.Column("locations", JSONB(), nullable=True),
        sa.Column("company_key", sa.Text(), nullable=True),
        sa.Column("jobs_url", sa.Text(), nullable=True),
        sa.Column("poll", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("agency", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("remote_open_jobs", sa.Integer(), nullable=True),
        sa.Column("remote_locked_jobs", sa.Integer(), nullable=True),
        sa.Column("posted_24h", sa.Integer(), nullable=True),
        sa.Column("posted_7d", sa.Integer(), nullable=True),
        sa.Column("target_posted_7d", sa.Integer(), nullable=True),
        sa.Column("fits_7d", sa.Integer(), nullable=True),
        sa.Column("last_posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ats_boards_state", "ats_boards", ["state", "india_job_count"])
    op.create_index("ix_ats_boards_poll", "ats_boards", ["poll", "ats"])
    op.create_index("ix_ats_boards_company_key", "ats_boards", ["company_key"])


def downgrade() -> None:
    op.drop_index("ix_ats_boards_company_key", table_name="ats_boards", if_exists=True)
    op.drop_index("ix_ats_boards_poll", table_name="ats_boards", if_exists=True)
    op.drop_index("ix_ats_boards_state", table_name="ats_boards")
    op.drop_table("ats_boards")
    op.drop_index("ix_ats_probes_pending", table_name="ats_probes")
    op.drop_table("ats_probes")
