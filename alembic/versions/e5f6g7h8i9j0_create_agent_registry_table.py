"""create agent_registry table

Revision ID: e5f6g7h8i9j0
Revises: c4d5e6f7g8h9
Create Date: 2026-05-24
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6g7h8i9j0'
down_revision = 'c4d5e6f7g8h9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'agent_registry',
        sa.Column('agent_name', sa.String(255), primary_key=True),
        sa.Column('enabled', sa.Integer(), nullable=False, server_default='1'),
    )


def downgrade() -> None:
    op.drop_table('agent_registry')
