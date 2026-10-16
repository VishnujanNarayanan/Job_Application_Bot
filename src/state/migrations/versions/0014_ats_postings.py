"""ATS postings the poller has seen, and when each board was last polled (#97).

``ats_postings`` holds one row per posting found on a polled board: when it was
first and last seen, and when it dropped out of the board's feed (closed). A
posting already in it is not processed again, so each hourly poll handles
only what is new. ``ats_boards`` gains ``polled_at`` / ``poll_error`` (a
failing board is visible) and ``tier`` / ``next_poll_at``: hot boards are
polled hourly, active ones every 3 hours, quiet ones every 12.

Revision ID: 0014_ats_postings
Revises: 0012_ats_boards
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_ats_postings"
down_revision: Union[str, None] = "0012_ats_boards"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ats_postings",
        sa.Column("ats", sa.Text(), primary_key=True),
        sa.Column("board_id", sa.Text(), primary_key=True),
        sa.Column("posting_id", sa.Text(), primary_key=True),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("place", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        # Why it was not sent on to the pipeline (place, title, level), or
        # null when it was.
        sa.Column("skipped", sa.Text(), nullable=True),
    )
    op.create_index("ix_ats_postings_job_id", "ats_postings", ["job_id"])
    op.create_index("ix_ats_postings_first_seen", "ats_postings", ["first_seen_at"])
    op.add_column("ats_boards", sa.Column("polled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ats_boards", sa.Column("poll_error", sa.Text(), nullable=True))
    op.add_column("ats_boards", sa.Column("tier", sa.Text(), nullable=True))
    op.add_column("ats_boards", sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_ats_boards_due", "ats_boards", ["poll", "next_poll_at"])


def downgrade() -> None:
    op.drop_index("ix_ats_boards_due", table_name="ats_boards")
    op.drop_column("ats_boards", "next_poll_at")
    op.drop_column("ats_boards", "tier")
    op.drop_column("ats_boards", "poll_error")
    op.drop_column("ats_boards", "polled_at")
    op.drop_index("ix_ats_postings_first_seen", table_name="ats_postings")
    op.drop_index("ix_ats_postings_job_id", table_name="ats_postings")
    op.drop_table("ats_postings")
