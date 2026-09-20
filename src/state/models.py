"""Layer 7 — SQLAlchemy 2.0 ORM models for every Postgres table.

Tables and indexes mirror architecture doc Section 7.3 exactly. Vector
columns are ``pgvector`` ``vector(384)`` (matches the all-MiniLM-L6-v2
embedding dimension); ivfflat cosine indexes are created in the Alembic
migration alongside the tables.

Iteration 1: synchronous SQLAlchemy. Async session swap is planned for
Iteration 2 when Gemini-parallelism and submission concurrency arrive.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Common declarative base for every table."""


EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Scraped + outcome tables
# ---------------------------------------------------------------------------


class AllJobs(Base):
    __tablename__ = "all_jobs"

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    company: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    site: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    job_url: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    jd_text: Mapped[str | None] = mapped_column(Text)
    jd_embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    required_skills: Mapped[Any | None] = mapped_column(JSONB)
    nice_to_have: Mapped[Any | None] = mapped_column(JSONB)
    responsibilities: Mapped[Any | None] = mapped_column(JSONB)
    role_summary: Mapped[str | None] = mapped_column(Text)
    team_or_product: Mapped[str | None] = mapped_column(Text)
    years_required: Mapped[int | None] = mapped_column(Integer)
    role_level: Mapped[str | None] = mapped_column(Text)
    role_category: Mapped[str | None] = mapped_column(Text)
    job_type: Mapped[str | None] = mapped_column(Text)
    location_type: Mapped[str | None] = mapped_column(Text)
    salary_min_lpa: Mapped[float | None] = mapped_column(Float)
    salary_max_lpa: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    # The original this JD duplicates (>0.95 cosine). Self-referential FK:
    # the DB guarantees the target row exists, so the scraper must insert +
    # commit the original before linking a near-duplicate to it.
    near_duplicate_of: Mapped[str | None] = mapped_column(
        Text, ForeignKey("all_jobs.job_id")
    )
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    id: Mapped[int] = mapped_column(
        BigInteger,
        server_default=sa_text("nextval('all_jobs_id_seq')"),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_all_jobs_scraped", scraped_at.desc()),
        Index("idx_all_jobs_outcome", "outcome"),
        Index(
            "idx_all_jobs_skills",
            "required_skills",
            postgresql_using="gin",
        ),
    )


class Applied(Base):
    """Matched jobs: a resume selection was built and the user notified.

    Pivot reshape (architecture §7.3): the auto-apply columns are gone
    (apply_type, resume/cover S3 URIs, cover_letter_used,
    application_status, failure_reason). The durable per-job artifact is
    ``selection_json`` (~2 KB); the PDF/DOCX is rendered on demand.
    ``user_status`` is set by the operator via Telegram/Sheet.
    """

    __tablename__ = "applied"

    job_id: Mapped[str] = mapped_column(
        Text, ForeignKey("all_jobs.job_id"), primary_key=True
    )
    selection_json: Mapped[Any | None] = mapped_column(JSONB)
    template_version: Mapped[str | None] = mapped_column(Text)
    cover_letter_text: Mapped[str | None] = mapped_column(Text)
    expected_salary_lpa: Mapped[float | None] = mapped_column(Float)
    fit_score: Mapped[float | None] = mapped_column(Float)
    success_prob: Mapped[float | None] = mapped_column(Float)
    recency_score: Mapped[float | None] = mapped_column(Float)
    final_score: Mapped[float | None] = mapped_column(Float)
    gap_skills: Mapped[Any | None] = mapped_column(JSONB)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # pending | applied | dismissed — set by the operator from the dashboard,
    # which asks "did you apply?" when they return from the job posting.
    user_status: Mapped[str] = mapped_column(Text, default="pending")
    # When user_status last changed. NULL means never actioned; it is not
    # backfilled from built_at, which is when the resume was built rather than
    # when the operator acted.
    user_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    built_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NotApplied(Base):
    __tablename__ = "not_applied"

    job_id: Mapped[str] = mapped_column(
        Text, ForeignKey("all_jobs.job_id"), primary_key=True
    )
    reason_category: Mapped[str] = mapped_column(Text, nullable=False)
    reason_detail: Mapped[str | None] = mapped_column(Text)
    fit_score: Mapped[float | None] = mapped_column(Float)
    success_prob: Mapped[float | None] = mapped_column(Float)
    recency_score: Mapped[float | None] = mapped_column(Float)
    final_score: Mapped[float | None] = mapped_column(Float)
    gap_skills: Mapped[Any | None] = mapped_column(JSONB)
    in_field: Mapped[bool | None] = mapped_column(Boolean)
    not_applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Queues
# ---------------------------------------------------------------------------


class ProcessingQueue(Base):
    __tablename__ = "processing_queue"

    job_id: Mapped[str] = mapped_column(
        Text, ForeignKey("all_jobs.job_id"), primary_key=True
    )
    status: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Master profile (NEVER hard-deleted)
# ---------------------------------------------------------------------------


class MasterBullets(Base):
    __tablename__ = "master_bullets"

    bullet_id: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_type: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[Any | None] = mapped_column(JSONB)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Role-block provenance. A bullet now belongs to one role_block of one entry
    # ("{entry_id}::{role}"), the selector pools an entry's blocks and pins
    # bullet_index 0 of the lead block, and extra_bullets are the recovery pool
    # that only renders when a JD asks for their keyword.
    block_id: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    bullet_index: Mapped[int | None] = mapped_column(Integer)
    is_summary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_extra: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_bullets_parent", "parent_id", "parent_type"),
        Index("idx_bullets_active", "is_active"),
        Index("idx_bullets_block", "block_id"),
    )


class MasterSummaries(Base):
    __tablename__ = "master_summaries"

    summary_id: Mapped[str] = mapped_column(Text, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[Any | None] = mapped_column(JSONB)
    role_categories: Mapped[Any | None] = mapped_column(JSONB)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MasterTitleAliases(Base):
    __tablename__ = "master_title_aliases"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Aliases hang off the role_block, not the entry: each block targets its own
    # title family, so the `data` block's aliases are not the `backend` block's.
    block_id: Mapped[str | None] = mapped_column(Text)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        Index("idx_aliases_parent", "parent_id"),
        Index("idx_aliases_block", "block_id"),
    )


class MasterMeta(Base):
    __tablename__ = "master_meta"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Runtime / housekeeping
# ---------------------------------------------------------------------------


class SearchRotationState(Base):
    __tablename__ = "search_rotation_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class PortalHealth(Base):
    __tablename__ = "portal_health"

    site: Mapped[str] = mapped_column(Text, primary_key=True)
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_count: Mapped[int | None] = mapped_column(Integer)
    consecutive_zeros: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class CompanyCooldown(Base):
    __tablename__ = "company_cooldown"

    company: Mapped[str] = mapped_column(Text, primary_key=True)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Render cache — the PDF/DOCX files live in S3; this table tracks them
# (architecture §7.3). Cache key is {job_id}_{template_version}_{ext}.
# ---------------------------------------------------------------------------


class RenderCache(Base):
    __tablename__ = "render_cache"

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("all_jobs.job_id")
    )
    format: Mapped[str | None] = mapped_column(Text)  # pdf | docx
    template_version: Mapped[str | None] = mapped_column(Text)
    s3_uri: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("idx_render_cache_expiry", "expires_at"),)


# ---------------------------------------------------------------------------
# Layer 3 evaluation — what the model was given and what it gave back.
# Not pipeline state: nothing in a run reads this back. It exists so a change
# to the parser can be compared over the same listings instead of judged from
# a handful of parses read off the terminal.
# ---------------------------------------------------------------------------


class ParseEval(Base):
    __tablename__ = "parse_eval"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Deliberately not a ForeignKey — an eval may cover a listing that was
    # never persisted, and deleting job rows must not erase measurements.
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    variant: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float)
    max_skill_chars: Mapped[int | None] = mapped_column(Integer)
    # The prompt as sent, not the raw ad: clipping is per-provider and the
    # instruction block changes as it is tuned, so re-deriving it later would
    # compare against a prompt that no longer exists.
    prompt_sent: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    jd_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    output_json: Mapped[Any | None] = mapped_column(JSONB)
    # Every skill this parse discarded, with the rule that did it. Job ads vary
    # enough that no rejection rule can be assumed safe; keeping the discards
    # is what makes one auditable over a corpus.
    rejections: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_parse_eval_variant_job", "variant", "job_id"),
        Index("ix_parse_eval_created_at", "created_at"),
    )


# ---------------------------------------------------------------------------
# Layer 3 vocabulary — technology terms learned from the corpus of parsed JDs.
# The model under-reports; the operator's own pool covers what affects the
# match score, and this covers the rest (gap skills for Familiar With).
# ---------------------------------------------------------------------------


class SkillVocabulary(Base):
    __tablename__ = "skill_vocabulary"

    term_key: Mapped[str] = mapped_column(Text, primary_key=True)
    term: Mapped[str] = mapped_column(Text, nullable=False)
    # Distinct jobs the term was extracted from — the signal separating a real
    # technology from a hallucination, which does not recur across unrelated ads.
    job_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Retire a bad entry by hand without deleting it, so a rebuild honours it.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_skill_vocabulary_active", "is_active", "job_count"),)
