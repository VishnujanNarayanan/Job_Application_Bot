"""Add skill_vocabulary — technology terms learned from the corpus of parsed JDs.

Revision ID: 0008_skill_vocabulary
Revises: 0007_parse_eval_rejections
Create Date: 2026-08-19

The model under-reports. Measured 2026-08-19 over four real ads, every one of
LangChain, LangGraph, MLOps, NLP, BERT, GPT, Mistral, TinyML, RAG and Scikit
was present in the JD text and absent from the parse. `with_pool_skills`
already guarantees the operator's own skills survive that, because their pool
is a known list — but a technology outside the pool has nothing to check
against, and those are exactly the gap skills Familiar With is built from.

The corpus is that missing list. Across 767 parsed jobs the parser has emitted
4,577 distinct skill strings, and the ones that recur across many unrelated ads
are real: a hallucination does not appear in thirty different listings. Terms
above a recurrence floor become a vocabulary the JD can be scanned against
deterministically, with no model involved.

Stored rather than computed per run so a run does not aggregate the whole
corpus, and so the vocabulary is inspectable and correctable by hand.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_skill_vocabulary"
down_revision: Union[str, None] = "0007_parse_eval_rejections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skill_vocabulary",
        # The term as it should be written into required_skills, lower-cased
        # for lookup. Casing is kept separately so "PyTorch" is not emitted as
        # "pytorch" once a JD happens to spell it that way.
        sa.Column("term_key", sa.Text(), primary_key=True),
        sa.Column("term", sa.Text(), nullable=False),
        # How many distinct jobs the term was extracted from. This is the
        # signal that separates a technology from a hallucination.
        sa.Column("job_count", sa.Integer(), nullable=False),
        # False disables a term without deleting it, so a bad entry can be
        # retired by hand and stay retired across rebuilds.
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_skill_vocabulary_active", "skill_vocabulary", ["is_active", "job_count"]
    )


def downgrade() -> None:
    op.drop_index("ix_skill_vocabulary_active", table_name="skill_vocabulary")
    op.drop_table("skill_vocabulary")
