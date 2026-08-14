"""add vless_service_short_uuid to managed_pools

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'd5e6f7a8b9c0'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'managed_pools',
        sa.Column('vless_service_short_uuid', sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('managed_pools', 'vless_service_short_uuid')
