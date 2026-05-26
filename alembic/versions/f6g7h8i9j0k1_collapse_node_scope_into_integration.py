"""collapse node scope into integration

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-05-25

The "node" scope was functionally identical to "integration" — the secrets
table has no node_id column, so a node-scoped row was stored exactly like
an integration-scoped one. Three keys ever used scope="node" across the
ecosystem (SPOTIFY_DEVICE_NAME, OPENWEATHER_LOCATION, LAN_SUBNET); this
migration moves them to "integration" so we can drop the dead axis.

Edge case: if a node-scoped row and an integration-scoped row already exist
for the same key (no user_id), prefer the integration row and delete the
node row. Unlikely in practice but cheap defense.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f6g7h8i9j0k1'
down_revision: Union[str, None] = 'e5f6g7h8i9j0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM secrets
        WHERE scope = 'node'
          AND key IN (
              SELECT key FROM secrets
              WHERE scope = 'integration' AND user_id IS NULL
          )
        """
    )
    op.execute("UPDATE secrets SET scope = 'integration' WHERE scope = 'node'")


def downgrade() -> None:
    # No-op: we can't restore which integration rows were originally "node"
    # scoped, and the new code path treats them all as integration anyway.
    pass
