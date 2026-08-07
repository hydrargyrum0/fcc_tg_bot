import asyncio
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from db.models.base import Base
from db.models.user import User  # noqa: F401 — ensures model is registered
from db.models.team import Team  # noqa: F401
from db.models.team_member import TeamMember  # noqa: F401
from db.models.remnawave_panel import RemnaWavePanel  # noqa: F401
from db.models.cloudflare_settings import CloudflareSettings  # noqa: F401
from db.models.aws_account import AWSAccount  # noqa: F401
from db.models.organization import Organization  # noqa: F401
from db.models.organization_member import OrganizationMember  # noqa: F401
from db.models.ip_set import IpSet  # noqa: F401
from db.models.pingachock_settings import PingachockSettings  # noqa: F401
from db.models.automation_group import AutomationGroup  # noqa: F401
from config import settings

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
