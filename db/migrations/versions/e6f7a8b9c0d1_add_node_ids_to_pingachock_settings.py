"""add node_ids to pingachock_settings

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'e6f7a8b9c0d1'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'pingachock_settings',
        sa.Column('node_ids', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('pingachock_settings', 'node_ids')
