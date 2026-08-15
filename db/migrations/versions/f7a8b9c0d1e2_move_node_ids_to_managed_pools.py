"""move node_ids from pingachock_settings to managed_pools

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'f7a8b9c0d1e2'
down_revision = 'e6f7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add node_ids to managed_pools (per-pool node filter)
    op.add_column(
        'managed_pools',
        sa.Column('node_ids', sa.JSON(), nullable=True),
    )
    # Remove from pingachock_settings (was a global setting — wrong level)
    op.drop_column('pingachock_settings', 'node_ids')


def downgrade() -> None:
    op.add_column(
        'pingachock_settings',
        sa.Column('node_ids', sa.JSON(), nullable=True),
    )
    op.drop_column('managed_pools', 'node_ids')
