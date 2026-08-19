"""Add parse_eval.rejections — the skills a run discarded, and why.

Revision ID: 0007_parse_eval_rejections
Revises: 0006_parse_eval
Create Date: 2026-08-19

Every skill the parser drops was previously discarded silently: boilerplate,
over-length fragments, bare qualifiers. Job ads vary enough that no rule can be
assumed safe, and a silent rejection cannot be audited — the only honest way to
know whether a rule is costing real skills is to keep what it threw away and
look at it.

Stored per parse alongside the prompt and the output, so a rule can be judged
over a corpus rather than argued about.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_parse_eval_rejections"
down_revision: Union[str, None] = "0006_parse_eval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parse_eval", sa.Column("rejections", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("parse_eval", "rejections")
