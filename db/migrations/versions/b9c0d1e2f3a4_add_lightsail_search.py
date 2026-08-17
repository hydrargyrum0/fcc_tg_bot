"""add lightsail search tables

Revision ID: b9c0d1e2f3a4
Revises: f7a8b9c0d1e2
Create Date: 2026-08-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'b9c0d1e2f3a4'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'lightsail_region_configs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('org_id', sa.Integer(),
                  sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('aws_account_id', sa.Integer(),
                  sa.ForeignKey('aws_accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('region', sa.String(50), nullable=False),
        sa.Column('region_display_name', sa.String(100), nullable=False, server_default=''),
        sa.Column('status', sa.String(20), nullable=False, server_default='idle'),
        sa.Column('target_count', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('recheck_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('node_ids', sa.JSON(), nullable=True),
        sa.Column('total_checked', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('search_started_at', sa.DateTime(), nullable=True),
        sa.Column('last_recheck_at', sa.DateTime(), nullable=True),
        sa.Column('instance_name', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('aws_account_id', 'region', name='uq_lightsail_account_region'),
    )

    op.create_table(
        'lightsail_static_ips',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('config_id', sa.Integer(),
                  sa.ForeignKey('lightsail_region_configs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('static_ip_name', sa.String(100), nullable=False),
        sa.Column('ip_address', sa.String(45), nullable=False),
        sa.Column('is_working', sa.Boolean(), nullable=True),
        sa.Column('is_attached', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('tested_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('lightsail_static_ips')
    op.drop_table('lightsail_region_configs')
