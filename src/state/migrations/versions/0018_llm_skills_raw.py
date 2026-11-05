"""The LLM's own skills per job, so the vocabulary stops learning from itself (#188).

``vocabulary.rebuild`` counted the stored ``required_skills``, which already
hold the terms the vocabulary added to each parse. A term once in the
vocabulary was counted again from every advert that contains it ("make" in
746 stored lists, "quality" in 1,256), so junk could never drop out.

``all_jobs.llm_skills_raw`` keeps the model's own skills (grounded, before the
pool and vocabulary additions). The rebuild counts only this column; rows
parsed before it existed are not counted again. Nullable, no backfill: the
additions cannot be separated from older stored lists.

Revision ID: 0018_llm_skills_raw
Revises: 0017_applied_telegram_message
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018_llm_skills_raw"
down_revision: Union[str, None] = "0017_applied_telegram_message"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("all_jobs", sa.Column("llm_skills_raw", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("all_jobs", "llm_skills_raw")
