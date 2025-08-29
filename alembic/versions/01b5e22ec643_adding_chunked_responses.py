"""adding chunked responses

Revision ID: 01b5e22ec643
Revises: 0aba36c02b13
Create Date: 2025-08-17 17:39:44.335260

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


# revision identifiers, used by Alembic.
revision: str = '01b5e22ec643'
down_revision: Union[str, None] = '0aba36c02b13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create chunked_command_responses table
    op.create_table(
        'chunked_command_responses',
        sa.Column('id', sa.String(36), primary_key=True),  # UUID as string for SQLite
        sa.Column('command_name', sa.String(255), nullable=False),
        sa.Column('session_id', sa.String(255), nullable=False, unique=True),
        sa.Column('full_content', sa.Text, nullable=False, default=''),
        sa.Column('created_at', sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('updated_at', sa.DateTime, nullable=False, server_default=sa.func.current_timestamp())
    )
    
    # Create indexes for efficient querying
    op.create_index('idx_chunked_command_responses_session_id', 'chunked_command_responses', ['session_id'])
    op.create_index('idx_chunked_command_responses_command_name', 'chunked_command_responses', ['command_name'])
    op.create_index('idx_chunked_command_responses_updated_at', 'chunked_command_responses', ['updated_at'])


def downgrade() -> None:
    """Downgrade schema."""
    # Drop indexes
    op.drop_index('idx_chunked_command_responses_updated_at', 'chunked_command_responses')
    op.drop_index('idx_chunked_command_responses_command_name', 'chunked_command_responses')
    op.drop_index('idx_chunked_command_responses_session_id', 'chunked_command_responses')
    
    # Drop table
    op.drop_table('chunked_command_responses')
