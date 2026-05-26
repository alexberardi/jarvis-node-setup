"""create disabled_fast_paths table

Revision ID: g7h8i9j0k1l2
Revises: f6g7h8i9j0k1
Create Date: 2026-05-26

Sparse opt-out table for IJarvisCommand.fast_path_patterns. A row exists
only when a user has disabled a specific (command_name, pattern_id) from
the mobile inspect UI; everything else defaults to enabled. Composite PK
on (command_name, pattern_id) so the same pattern_id can exist locally
under different commands without collision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'g7h8i9j0k1l2'
down_revision: Union[str, None] = 'f6g7h8i9j0k1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'disabled_fast_paths',
        sa.Column('command_name', sa.String(255), primary_key=True),
        sa.Column('pattern_id', sa.String(255), primary_key=True),
        sa.Column(
            'disabled_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table('disabled_fast_paths')
