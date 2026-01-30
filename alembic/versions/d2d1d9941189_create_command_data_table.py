"""create command_data table

Revision ID: d2d1d9941189
Revises: 01b5e22ec643
Create Date: 2026-01-29 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2d1d9941189'
down_revision: Union[str, None] = '01b5e22ec643'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create command_data table for generic command persistence."""
    # SQLite requires constraints to be defined in CREATE TABLE
    # (no ALTER TABLE ADD CONSTRAINT support)
    op.create_table(
        'command_data',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('command_name', sa.String(255), nullable=False),
        sa.Column('data_key', sa.String(255), nullable=False),
        sa.Column('data', sa.Text, nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        # Unique constraint must be in CREATE TABLE for SQLite
        sa.UniqueConstraint('command_name', 'data_key', name='uq_command_data_key'),
    )

    # Create indexes for efficient querying
    op.create_index(
        'idx_command_data_command_name',
        'command_data',
        ['command_name']
    )
    op.create_index(
        'idx_command_data_expires_at',
        'command_data',
        ['expires_at']
    )


def downgrade() -> None:
    """Drop command_data table."""
    op.drop_index('idx_command_data_expires_at', 'command_data')
    op.drop_index('idx_command_data_command_name', 'command_data')
    op.drop_table('command_data')
