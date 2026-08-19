"""Add parse_eval — what the LLM was given and what it returned, per attempt.

Revision ID: 0006_parse_eval
Revises: 0005_user_status_at
Create Date: 2026-08-19

Layer 3 changes were being judged by reading a handful of parses off the
terminal. That is how a 25% failure rate went unnoticed for months: the model
extracted cleanly on most JDs, so a spot check looked fine while a quarter of
listings were losing every technology they named.

This table stores the prompt actually sent and the object actually returned,
per variant, so two configurations can be compared over the same listings
rather than argued about. It is an evaluation record, not pipeline state —
nothing in the run reads it back, and dropping it costs only history.

`prompt_sent` is stored in full rather than referenced through
``all_jobs.jd_text``: the prompt is what the model saw, and it differs from the
raw ad by clipping (which is per-provider) and by the instruction block (which
changes as it is tuned). Re-deriving it later would compare against a prompt
that no longer exists.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_parse_eval"
down_revision: Union[str, None] = "0005_user_status_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parse_eval",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        # No FK to all_jobs: an eval may be run over a listing that was never
        # persisted (a fixture, a re-scrape), and a cascade delete of job rows
        # should not silently erase the measurement history.
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("max_skill_chars", sa.Integer(), nullable=True),
        sa.Column("prompt_sent", sa.Text(), nullable=False),
        sa.Column("prompt_chars", sa.Integer(), nullable=False),
        sa.Column("jd_chars", sa.Integer(), nullable=False),
        # Whole parsed object, not just the skills: role_level and
        # years_required drive 30% of the final score, so a change that fixes
        # skills while degrading those is a regression that must be visible.
        sa.Column("output_json", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # The comparison query is "this variant against that one, over the same
    # listings", so variant leads and job_id follows.
    op.create_index(
        "ix_parse_eval_variant_job", "parse_eval", ["variant", "job_id"], unique=False
    )
    op.create_index("ix_parse_eval_created_at", "parse_eval", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_parse_eval_created_at", table_name="parse_eval")
    op.drop_index("ix_parse_eval_variant_job", table_name="parse_eval")
    op.drop_table("parse_eval")
