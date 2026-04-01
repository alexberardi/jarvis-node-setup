"""add user_id to secrets

Revision ID: c4d5e6f7g8h9
Revises: b3c4d5e6f7g8
Create Date: 2026-03-31 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7g8h9'
down_revision: Union[str, None] = 'b3c4d5e6f7g8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('secrets', sa.Column('user_id', sa.Integer(), nullable=True))
    op.create_index('ix_secrets_key_scope_user', 'secrets', ['key', 'scope', 'user_id'])


def downgrade() -> None:
    op.drop_index('ix_secrets_key_scope_user', table_name='secrets')
    op.drop_column('secrets', 'user_id')
