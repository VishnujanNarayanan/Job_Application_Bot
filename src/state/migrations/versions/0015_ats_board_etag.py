"""Each board's last list validator, for conditional polls (#112).

Greenhouse, Lever, Ashby and SmartRecruiters all send an ``ETag`` on their
list endpoints and answer ``If-None-Match`` with an empty ``304 Not
Modified`` when nothing changed (checked 2026-10-17). ``ats_boards.etag``
keeps the last one, so an unchanged board costs no body download, no parse
and no bookkeeping. Nullable: a board never polled, or one whose list must
be read in full next time, has none.

Revision ID: 0015_ats_board_etag
Revises: 0014_ats_postings
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_ats_board_etag"
down_revision: Union[str, None] = "0014_ats_postings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ats_boards", sa.Column("etag", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ats_boards", "etag")
