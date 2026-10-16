"""Which parser decided each job, and the dictionary score behind it (#100, #71).

Every job reaching Layer 3 is now parsed and scored from the skill
dictionary first (no LLM). Below the parse floor it is decided from that
alone; otherwise the LLM parses it as before. These columns record the route
and keep the dictionary score for every routed job, so the floor and the #71
cut-offs can be checked and re-derived from live data. An index on
``routed_at`` serves the daily LLM-call count. All nullable: rows parsed
before this migration were never routed.

Revises 0012_ats_boards (#97), which is already applied to the database, so
#97 must merge before this.

Revision ID: 0013_parse_route
Revises: 0012_ats_boards
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_parse_route"
down_revision: Union[str, None] = "0012_ats_boards"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("parse_mode", sa.Text()),
    ("parse_route_reason", sa.Text()),
    ("dict_score", sa.Float()),
    ("dict_fit", sa.Float()),
    ("dict_years", sa.Integer()),
    ("dict_job_type", sa.Text()),
    ("llm_called", sa.Boolean()),
    ("routing_version", sa.Text()),
    ("routed_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("all_jobs", sa.Column(name, type_, nullable=True))
    op.create_index("ix_all_jobs_routed_at", "all_jobs", ["routed_at"])


def downgrade() -> None:
    op.drop_index("ix_all_jobs_routed_at", table_name="all_jobs")
    for name, _type in reversed(_COLUMNS):
        op.drop_column("all_jobs", name)
