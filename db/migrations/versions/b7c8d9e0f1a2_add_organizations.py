"""add organizations system

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-01 01:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. organizations table
    op.create_table(
        'organizations',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(255), nullable=False, unique=True),
        sa.Column('created_by', sa.BigInteger(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    # 2. organization_members table
    op.create_table(
        'organization_members',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('joined_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('org_id', 'user_id', name='uq_org_member'),
    )
    # 3. users: add active_org_id and notified_no_access
    op.add_column('users', sa.Column('active_org_id', sa.BigInteger(), nullable=True))
    op.add_column('users', sa.Column('notified_no_access', sa.Boolean(), nullable=False, server_default='false'))
    # 4. remnawave_panels: add org_id, drop user_id
    op.add_column('remnawave_panels', sa.Column('org_id', sa.Integer(), nullable=True))
    op.drop_constraint('remnawave_panels_user_id_fkey', 'remnawave_panels', type_='foreignkey')
    op.drop_column('remnawave_panels', 'user_id')
    # 5. cloudflare_settings: add org_id, drop user_id (with its unique constraint)
    op.add_column('cloudflare_settings', sa.Column('org_id', sa.Integer(), nullable=True))
    op.drop_constraint('cloudflare_settings_user_id_key', 'cloudflare_settings', type_='unique')
    op.drop_constraint('cloudflare_settings_user_id_fkey', 'cloudflare_settings', type_='foreignkey')
    op.drop_column('cloudflare_settings', 'user_id')
    op.create_unique_constraint('uq_cloudflare_settings_org', 'cloudflare_settings', ['org_id'])
    # 6. aws_accounts: add org_id, drop user_id
    op.add_column('aws_accounts', sa.Column('org_id', sa.Integer(), nullable=True))
    op.drop_constraint('aws_accounts_user_id_fkey', 'aws_accounts', type_='foreignkey')
    op.drop_column('aws_accounts', 'user_id')


def downgrade() -> None:
    op.add_column('aws_accounts', sa.Column('user_id', sa.BigInteger(), nullable=True))
    op.add_column('cloudflare_settings', sa.Column('user_id', sa.BigInteger(), nullable=True))
    op.add_column('remnawave_panels', sa.Column('user_id', sa.BigInteger(), nullable=True))
    op.drop_column('aws_accounts', 'org_id')
    op.drop_column('cloudflare_settings', 'org_id')
    op.drop_column('remnawave_panels', 'org_id')
    op.drop_column('users', 'notified_no_access')
    op.drop_column('users', 'active_org_id')
    op.drop_table('organization_members')
    op.drop_table('organizations')
