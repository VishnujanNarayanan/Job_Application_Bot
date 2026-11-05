"""The Telegram message a match was sent as (#186).

A late sibling of a sent match (the same role, another city or another
opening) edits that message instead of sending a new one, and a new city
gets a silent reply threaded under it. Both need the message's id, and the
edit needs its text and buttons (Telegram drops the buttons of an edited
message unless they are sent again).

``applied.telegram_message``: {"id": int, "text": str, "markup": dict | null},
the text without its sibling lines. Null for matches sent before this.

Revision ID: 0017_applied_telegram_message
Revises: 0016_ats_posting_status
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_applied_telegram_message"
down_revision: Union[str, None] = "0016_ats_posting_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("applied", sa.Column("telegram_message", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("applied", "telegram_message")
