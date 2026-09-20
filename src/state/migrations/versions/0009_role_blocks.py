"""Role-block-shaped master profile (Headless template pivot).

The profile stopped being ``{entry -> flat bullet_pool}`` and became
``{entry -> role_blocks[] -> ordered bullets + a recovery pool}``, where
``bullets[0]`` of a block is that entry's plain-language summary bullet. Selection
now pools an entry's blocks and pins that bullet, so a row has to know which block
it came from, its ordinal within the render set, whether it is the summary, and
whether it is recovery-pool-only. None of that was expressible in the flat schema.

``master_summaries`` is deliberately NOT dropped. Hard rule #17: ``selection_json``
v1 rows in ``applied`` reference ``summary_id`` and must keep resolving, and the
project has never hard-deleted profile content. The rows are deactivated instead —
the same treatment a bullet removed from the YAML gets.

Revision ID: 0009_role_blocks
Revises: 0008_skill_vocabulary
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_role_blocks"
down_revision: Union[str, None] = "0008_skill_vocabulary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("master_bullets", sa.Column("block_id", sa.Text(), nullable=True))
    op.add_column("master_bullets", sa.Column("role", sa.Text(), nullable=True))
    op.add_column("master_bullets", sa.Column("bullet_index", sa.Integer(), nullable=True))
    op.add_column(
        "master_bullets",
        sa.Column(
            "is_summary", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "master_bullets",
        sa.Column(
            "is_extra", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_index("idx_bullets_block", "master_bullets", ["block_id"])

    op.add_column("master_title_aliases", sa.Column("block_id", sa.Text(), nullable=True))
    op.create_index("idx_aliases_block", "master_title_aliases", ["block_id"])

    # The resume Summary section is gone: the method writes one only for an
    # industry change, a relocation or a visa, and the template has no place for
    # it. Deactivate, never delete (hard rule #17).
    op.execute("UPDATE master_summaries SET is_active = false")

    # project_name rows existed to feed the name-cosine term in the old
    # score_project. Projects are now scored through their role_block title
    # aliases exactly like work entries, so nothing desires these rows any more.
    op.execute(
        "UPDATE master_bullets SET is_active = false WHERE parent_type = 'project_name'"
    )


def downgrade() -> None:
    op.drop_index("idx_aliases_block", table_name="master_title_aliases")
    op.drop_column("master_title_aliases", "block_id")
    op.drop_index("idx_bullets_block", table_name="master_bullets")
    for column in ("is_extra", "is_summary", "bullet_index", "role", "block_id"):
        op.drop_column("master_bullets", column)
    # The is_active flips are not reversed. Reactivating rows the schema no longer
    # desires would be undone by the next rebuild anyway, and flipping them back
    # blind would resurrect summaries the operator has since removed.
