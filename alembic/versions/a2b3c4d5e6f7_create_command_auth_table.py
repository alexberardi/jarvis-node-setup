"""create command_auth table

Revision ID: a2b3c4d5e6f7
Revises: d2d1d9941189
Create Date: 2026-02-14 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'd2d1d9941189'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create command_auth table for tracking provider auth status."""
    op.create_table(
        'command_auth',
        sa.Column('provider', sa.String(255), primary_key=True),
        sa.Column('needs_auth', sa.Integer, nullable=False, server_default='0'),
        sa.Column('auth_error', sa.Text, nullable=True),
        sa.Column('last_checked_at', sa.Text, nullable=True),
        sa.Column('last_authed_at', sa.Text, nullable=True),
    )


def downgrade() -> None:
    """Drop command_auth table."""
    op.drop_table('command_auth')
