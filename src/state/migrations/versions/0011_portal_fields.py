"""Portal fields JobSpy returns that were dropped: direct apply link,
seniority label and experience band.

``job_level`` (LinkedIn) and ``experience_range`` (Naukri) are read before the
parse, so a director role or a "10-15 Yrs" band is rejected without an LLM
call. ``job_url_direct`` is the employer's own apply link, used for the Apply
button when the JD has none. All nullable: each comes from one portal only,
and rows scraped before this migration have none.

Revision ID: 0011_portal_fields
Revises: 0010_applicants
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_portal_fields"
down_revision: Union[str, None] = "0010_applicants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("all_jobs", sa.Column("job_url_direct", sa.Text(), nullable=True))
    op.add_column("all_jobs", sa.Column("job_level", sa.Text(), nullable=True))
    op.add_column("all_jobs", sa.Column("experience_range", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("all_jobs", "experience_range")
    op.drop_column("all_jobs", "job_level")
    op.drop_column("all_jobs", "job_url_direct")
