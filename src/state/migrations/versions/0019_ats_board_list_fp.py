"""Each board's last complete list, as a fingerprint of its posting ids.

Every poll used to load every known posting of every fetched board (8
columns of ``ats_postings``, the largest table) to compare against the new
lists. Most boards list exactly what they listed last time, so that read was
almost all repeat: about 40 MB per run, which used up the database host's
monthly network transfer (2026-11-06). ``list_fp`` lets the poll tell an
unchanged list from a changed one and load the full rows only for the
latter. Nullable: a board never read in full has none.

Revision ID: 0019_ats_board_list_fp
Revises: 0018_llm_skills_raw
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_ats_board_list_fp"
down_revision: Union[str, None] = "0018_llm_skills_raw"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ats_boards", sa.Column("list_fp", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ats_boards", "list_fp")
