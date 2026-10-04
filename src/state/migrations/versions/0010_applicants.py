"""LinkedIn applicant count on all_jobs.

success_prob is now the applicant score -- fewer applicants scores higher --
instead of time since posting, which the 1-hour scrape window made the same
for every job (issue #13). The count is read from the job page JobSpy already
fetches, so it costs no extra request. Both columns are nullable: only LinkedIn
shows a count, and rows scraped before this migration have none.

``recency_score`` on applied/not_applied is kept and still written: time since
posting is recorded for every job, it just no longer enters the score.

Revision ID: 0010_applicants
Revises: 0009_role_blocks
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_applicants"
down_revision: Union[str, None] = "0009_role_blocks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("all_jobs", sa.Column("applicants_text", sa.Text(), nullable=True))
    op.add_column("all_jobs", sa.Column("applicants_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("all_jobs", "applicants_count")
    op.drop_column("all_jobs", "applicants_text")
