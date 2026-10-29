"""Where each sent-on ATS posting is in the pipeline: pending, then done.

A posting that passes the free filters is recorded as seen and sent on. A run
that stopped after recording it but before saving its job to all_jobs
stranded it: the next poll saw it as known and never sent it again. On
2026-10-29 two interrupted runs stranded 1,316 postings that way.

``ats_postings.status`` is "pending" from the moment a posting is sent on and
"done" once its job is in all_jobs. A later run sends any posting still
"pending" again (src.scraper.ats_poller.requeue_pending). Null for skipped
postings. The backfill marks sent postings "done" when their job is in
all_jobs and "pending" when it isn't.

Revision ID: 0016_ats_posting_status
Revises: 0015_ats_board_etag
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_ats_posting_status"
down_revision: Union[str, None] = "0015_ats_board_etag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ats_postings", sa.Column("status", sa.Text(), nullable=True))
    op.execute(
        "UPDATE ats_postings p SET status = CASE WHEN EXISTS "
        "(SELECT 1 FROM all_jobs j WHERE j.job_id = p.job_id) THEN 'done' ELSE 'pending' END "
        "WHERE p.skipped IS NULL"
    )
    op.create_index(
        "ix_ats_postings_pending", "ats_postings", ["status"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_ats_postings_pending", table_name="ats_postings")
    op.drop_column("ats_postings", "status")
