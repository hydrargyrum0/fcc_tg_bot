"""add monitoring_enabled to remnawave_panels

Revision ID: a1b2c3d4e5f6
Revises: fcdb392fa44e
Create Date: 2026-07-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'fcdb392fa44e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'remnawave_panels',
        sa.Column('monitoring_enabled', sa.Boolean(), nullable=False, server_default='true'),
    )


def downgrade() -> None:
    op.drop_column('remnawave_panels', 'monitoring_enabled')
