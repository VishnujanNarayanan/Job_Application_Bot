"""Pivot schema — reshape `applied`, add `render_cache`, extend `all_jobs`.

Revision ID: 0003_pivot_schema
Revises: 0002_drop_autoapply
Create Date: 2026-06-03

Brings the schema to the post-pivot target (architecture §7.3):

  applied   drop the auto-apply columns (apply_type, resume/cover S3 URIs,
            cover_letter_used, application_status, failure_reason); rename
            applied_at -> built_at; add template_version, notified_at,
            user_status (default 'pending'). selection_json stays as the
            durable per-job artifact.
  all_jobs  add jd_embedding vector(384) + near_duplicate_of for
            near-duplicate detection (+ ivfflat cosine index).
  render_cache  new table tracking S3-backed PDF/DOCX renders (1-month TTL).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0003_pivot_schema"
down_revision: Union[str, None] = "0002_drop_autoapply"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 384

# Auto-apply columns removed from `applied` (name, type) — used by both
# directions so upgrade/downgrade stay in sync.
_DROPPED_APPLIED_COLUMNS = [
    ("apply_type", lambda: sa.Text()),
    ("resume_s3_uri", lambda: sa.Text()),
    ("resume_s3_key", lambda: sa.Text()),
    ("cover_letter_used", lambda: sa.Boolean()),
    ("cover_letter_s3_uri", lambda: sa.Text()),
    ("application_status", lambda: sa.Text()),
    ("failure_reason", lambda: sa.Text()),
]


def upgrade() -> None:
    # --- all_jobs: near-duplicate detection support --------------------
    op.add_column("all_jobs", sa.Column("jd_embedding", Vector(EMBEDDING_DIM)))
    op.add_column("all_jobs", sa.Column("near_duplicate_of", sa.Text()))
    # Self-referential FK: a near-duplicate must point at a job_id that
    # already exists. Layer 2's dedup is responsible for committing the
    # original before linking the duplicate (see src/scraper/dedup.py).
    op.create_foreign_key(
        "fk_all_jobs_near_duplicate_of",
        "all_jobs",
        "all_jobs",
        ["near_duplicate_of"],
        ["job_id"],
    )
    # Cosine ivfflat index. Valid to create on an empty table; rebuild later
    # for better recall once rows accumulate (REINDEX / drop+create).
    op.execute(
        "CREATE INDEX idx_all_jobs_embedding ON all_jobs "
        "USING ivfflat (jd_embedding vector_cosine_ops)"
    )

    # --- applied: drop auto-apply columns ------------------------------
    for name, _type in _DROPPED_APPLIED_COLUMNS:
        op.drop_column("applied", name)

    # Preserve existing timestamps by renaming rather than drop+add.
    op.alter_column("applied", "applied_at", new_column_name="built_at")

    op.add_column("applied", sa.Column("template_version", sa.Text()))
    op.add_column(
        "applied", sa.Column("notified_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "applied",
        sa.Column("user_status", sa.Text(), server_default="pending"),
    )

    # --- render_cache: tracks S3-backed renders ------------------------
    op.create_table(
        "render_cache",
        sa.Column("cache_key", sa.Text(), primary_key=True),
        sa.Column("job_id", sa.Text(), sa.ForeignKey("all_jobs.job_id")),
        sa.Column("format", sa.Text()),
        sa.Column("template_version", sa.Text()),
        sa.Column("s3_uri", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_render_cache_expiry", "render_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idx_render_cache_expiry", table_name="render_cache")
    op.drop_table("render_cache")

    op.drop_column("applied", "user_status")
    op.drop_column("applied", "notified_at")
    op.drop_column("applied", "template_version")
    op.alter_column("applied", "built_at", new_column_name="applied_at")
    # Re-add the auto-apply columns (cover_letter_used keeps its default).
    op.add_column("applied", sa.Column("apply_type", sa.Text()))
    op.add_column("applied", sa.Column("resume_s3_uri", sa.Text()))
    op.add_column("applied", sa.Column("resume_s3_key", sa.Text()))
    op.add_column(
        "applied",
        sa.Column("cover_letter_used", sa.Boolean(), server_default=sa.false()),
    )
    op.add_column("applied", sa.Column("cover_letter_s3_uri", sa.Text()))
    op.add_column("applied", sa.Column("application_status", sa.Text()))
    op.add_column("applied", sa.Column("failure_reason", sa.Text()))

    op.execute("DROP INDEX IF EXISTS idx_all_jobs_embedding")
    op.drop_constraint(
        "fk_all_jobs_near_duplicate_of", "all_jobs", type_="foreignkey"
    )
    op.drop_column("all_jobs", "near_duplicate_of")
    op.drop_column("all_jobs", "jd_embedding")
