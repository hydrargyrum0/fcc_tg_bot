"""add_managed_pools

Revision ID: c4d5e6f7a8b9
Revises: a2b3c4d5e6f7
Create Date: 2026-08-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'managed_pools',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('host_tag', sa.String(length=200), nullable=False),
        sa.Column('ip_set_ids', sa.JSON(), nullable=False),
        sa.Column('score_threshold', sa.Float(), nullable=False, server_default='60.0'),
        sa.Column('check_interval_minutes', sa.Integer(), nullable=False, server_default='120'),
        sa.Column('last_scanned_at', sa.DateTime(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'managed_ips',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('pool_id', sa.Integer(), nullable=False),
        sa.Column('ip', sa.String(length=45), nullable=False),
        sa.Column('score', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('is_approved', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('ping_rtt_ms', sa.Float(), nullable=True),
        sa.Column('ping_loss_pct', sa.Float(), nullable=True),
        sa.Column('tls_ok', sa.Boolean(), nullable=True),
        sa.Column('tls_handshake_ms', sa.Float(), nullable=True),
        sa.Column('vless_ok', sa.Boolean(), nullable=True),
        sa.Column('vless_speed_mbps', sa.Float(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['pool_id'], ['managed_pools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pool_id', 'ip', name='uq_managed_ips_pool_ip'),
    )

    op.add_column(
        'automation_groups',
        sa.Column('managed_pool_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_automation_groups_managed_pool_id',
        'automation_groups', 'managed_pools',
        ['managed_pool_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(
        'fk_automation_groups_managed_pool_id',
        'automation_groups',
        type_='foreignkey',
    )
    op.drop_column('automation_groups', 'managed_pool_id')
    op.drop_table('managed_ips')
    op.drop_table('managed_pools')
