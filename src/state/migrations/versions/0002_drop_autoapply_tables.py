"""Drop auto-apply tables removed in the Iteration 2 pivot.

Revision ID: 0002_drop_autoapply
Revises: 0001_initial
Create Date: 2026-06-03

The pivot removed auto-apply entirely (CLAUDE.md Migration Note). These
three tables only ever served the old Playwright submit flow and its
form-question/review machinery, none of which exists anymore:

  - application_queue  cycle-quota top-N picking + 12-hour decay
                       (replaced by "notify every match >= 0.50")
  - answer_bank        stored form-question answers (no form filling now)
  - pending_review     Telegram review of Cat-3 form answers (gone)

Forward-only by design: migration 0001 is left intact as the historical
record. ``downgrade`` recreates the tables so the migration is reversible,
but the application no longer references them.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_drop_autoapply"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF EXISTS guards: on a clean bootstrap 0001 creates these and 0002
    # drops them (always present, so a plain drop is safe). The guards are
    # belt-and-suspenders for a partial/hand-managed DB where a table was
    # already removed out-of-band — the migration stays idempotent either way.
    # (Postgres drops idx_queue_status with the table, but be explicit.)
    op.execute("DROP INDEX IF EXISTS idx_queue_status")
    op.execute("DROP TABLE IF EXISTS application_queue")
    op.execute("DROP TABLE IF EXISTS pending_review")
    op.execute("DROP TABLE IF EXISTS answer_bank")


def downgrade() -> None:
    # Recreate exactly as 0001 defined them, so an upgrade→downgrade round
    # trip restores the original schema.
    op.create_table(
        "answer_bank",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("question_patterns", postgresql.JSONB()),
        sa.Column("jd_contexts", postgresql.JSONB()),
        sa.Column("answer", sa.Text()),
        sa.Column("approved_by_user", sa.Boolean(), server_default=sa.false()),
        sa.Column("times_used", sa.Integer(), server_default="0"),
        sa.Column("last_used", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "pending_review",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("job_id", sa.Text()),
        sa.Column("company", sa.Text()),
        sa.Column("role", sa.Text()),
        sa.Column("question_text", sa.Text()),
        sa.Column("question_category", sa.Integer()),
        sa.Column("bank_match_id", sa.Text()),
        sa.Column("bank_match_score", sa.Float()),
        sa.Column("gemini_draft", sa.Text()),
        sa.Column("user_answer", sa.Text()),
        sa.Column("status", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "application_queue",
        sa.Column(
            "job_id",
            sa.Text(),
            sa.ForeignKey("all_jobs.job_id"),
            primary_key=True,
        ),
        sa.Column("final_score", sa.Float()),
        sa.Column("status", sa.Text()),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "idx_queue_status", "application_queue", ["status", "expires_at"]
    )
