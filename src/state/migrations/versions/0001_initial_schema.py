"""Initial schema — all 13 tables + pgvector.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-26

Mirrors architecture doc Section 7.3 exactly. Run AFTER ``CREATE EXTENSION
vector`` succeeds (Neon supports pgvector out of the box on every plan,
including free).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EMBEDDING_DIM = 384


def upgrade() -> None:
    # pgvector extension must exist before any column of type vector(N).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # -------------------------------------------------------------------
    # all_jobs — permanent scrape archive
    # -------------------------------------------------------------------
    op.create_table(
        "all_jobs",
        sa.Column("job_id", sa.Text(), primary_key=True),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("site", sa.Text(), nullable=False),
        sa.Column("location", sa.Text()),
        sa.Column("job_url", sa.Text()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "scraped_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("jd_text", sa.Text()),
        sa.Column("required_skills", postgresql.JSONB()),
        sa.Column("nice_to_have", postgresql.JSONB()),
        sa.Column("responsibilities", postgresql.JSONB()),
        sa.Column("role_summary", sa.Text()),
        sa.Column("team_or_product", sa.Text()),
        sa.Column("years_required", sa.Integer()),
        sa.Column("role_level", sa.Text()),
        sa.Column("role_category", sa.Text()),
        sa.Column("job_type", sa.Text()),
        sa.Column("location_type", sa.Text()),
        sa.Column("salary_min_lpa", sa.Float()),
        sa.Column("salary_max_lpa", sa.Float()),
        sa.Column("salary_currency", sa.Text()),
        sa.Column("outcome", sa.Text()),
        sa.Column("outcome_at", sa.DateTime(timezone=True)),
    )
    op.execute("CREATE INDEX idx_all_jobs_scraped ON all_jobs (scraped_at DESC)")
    op.create_index("idx_all_jobs_outcome", "all_jobs", ["outcome"])
    op.execute(
        "CREATE INDEX idx_all_jobs_skills ON all_jobs USING GIN (required_skills)"
    )

    # -------------------------------------------------------------------
    # applied — submission audit trail
    # -------------------------------------------------------------------
    op.create_table(
        "applied",
        sa.Column(
            "job_id",
            sa.Text(),
            sa.ForeignKey("all_jobs.job_id"),
            primary_key=True,
        ),
        sa.Column("apply_type", sa.Text()),
        sa.Column("resume_s3_uri", sa.Text()),
        sa.Column("resume_s3_key", sa.Text()),
        sa.Column("cover_letter_text", sa.Text()),
        sa.Column("cover_letter_used", sa.Boolean(), server_default=sa.false()),
        sa.Column("cover_letter_s3_uri", sa.Text()),
        sa.Column("selection_json", postgresql.JSONB()),
        sa.Column("expected_salary_lpa", sa.Float()),
        sa.Column("fit_score", sa.Float()),
        sa.Column("success_prob", sa.Float()),
        sa.Column("recency_score", sa.Float()),
        sa.Column("final_score", sa.Float()),
        sa.Column("gap_skills", postgresql.JSONB()),
        sa.Column("application_status", sa.Text()),
        sa.Column("failure_reason", sa.Text()),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # -------------------------------------------------------------------
    # not_applied — structured rejection reasons
    # -------------------------------------------------------------------
    op.create_table(
        "not_applied",
        sa.Column(
            "job_id",
            sa.Text(),
            sa.ForeignKey("all_jobs.job_id"),
            primary_key=True,
        ),
        sa.Column("reason_category", sa.Text(), nullable=False),
        sa.Column("reason_detail", sa.Text()),
        sa.Column("fit_score", sa.Float()),
        sa.Column("success_prob", sa.Float()),
        sa.Column("recency_score", sa.Float()),
        sa.Column("final_score", sa.Float()),
        sa.Column("gap_skills", postgresql.JSONB()),
        sa.Column("in_field", sa.Boolean()),
        sa.Column(
            "not_applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # -------------------------------------------------------------------
    # application_queue — 12-hour decay queue
    # -------------------------------------------------------------------
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

    # -------------------------------------------------------------------
    # processing_queue — transient per-run
    # -------------------------------------------------------------------
    op.create_table(
        "processing_queue",
        sa.Column(
            "job_id",
            sa.Text(),
            sa.ForeignKey("all_jobs.job_id"),
            primary_key=True,
        ),
        sa.Column("status", sa.Text()),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # -------------------------------------------------------------------
    # master_bullets — embedded, NEVER hard-deleted
    # -------------------------------------------------------------------
    op.create_table(
        "master_bullets",
        sa.Column("bullet_id", sa.Text(), primary_key=True),
        sa.Column("parent_id", sa.Text(), nullable=False),
        sa.Column("parent_type", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.JSONB()),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "idx_bullets_parent", "master_bullets", ["parent_id", "parent_type"]
    )
    op.create_index("idx_bullets_active", "master_bullets", ["is_active"])
    # ivfflat needs data to train — created post-insert in Iteration 2's
    # master_profile rebuild script (CREATE INDEX ... USING ivfflat ...).

    op.create_table(
        "master_summaries",
        sa.Column("summary_id", sa.Text(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.JSONB()),
        sa.Column("role_categories", postgresql.JSONB()),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "master_title_aliases",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("parent_id", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
    )
    op.create_index("idx_aliases_parent", "master_title_aliases", ["parent_id"])

    op.create_table(
        "master_meta",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text()),
    )

    # -------------------------------------------------------------------
    # Runtime / housekeeping
    # -------------------------------------------------------------------
    op.create_table(
        "search_rotation_state",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text()),
    )

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
        "portal_health",
        sa.Column("site", sa.Text(), primary_key=True),
        sa.Column("last_run", sa.DateTime(timezone=True)),
        sa.Column("result_count", sa.Integer()),
        sa.Column("consecutive_zeros", sa.Integer(), server_default="0"),
        sa.Column("last_error", sa.Text()),
    )

    op.create_table(
        "company_cooldown",
        sa.Column("company", sa.Text(), primary_key=True),
        sa.Column("last_applied_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    # Reverse order — children before parents, indexes before tables where
    # Alembic doesn't auto-drop them.
    op.drop_table("company_cooldown")
    op.drop_table("portal_health")
    op.drop_table("pending_review")
    op.drop_table("answer_bank")
    op.drop_table("search_rotation_state")
    op.drop_table("master_meta")
    op.drop_index("idx_aliases_parent", table_name="master_title_aliases")
    op.drop_table("master_title_aliases")
    op.drop_table("master_summaries")
    op.drop_index("idx_bullets_active", table_name="master_bullets")
    op.drop_index("idx_bullets_parent", table_name="master_bullets")
    op.drop_table("master_bullets")
    op.drop_table("processing_queue")
    op.drop_index("idx_queue_status", table_name="application_queue")
    op.drop_table("application_queue")
    op.drop_table("not_applied")
    op.drop_table("applied")
    op.drop_index("idx_all_jobs_skills", table_name="all_jobs")
    op.drop_index("idx_all_jobs_outcome", table_name="all_jobs")
    op.drop_index("idx_all_jobs_scraped", table_name="all_jobs")
    op.drop_table("all_jobs")
    # Leave the vector extension installed — other migrations or future
    # schemas may depend on it.
