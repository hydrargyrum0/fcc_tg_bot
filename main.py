import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from bot.handlers import (
    automation_fsm,
    aws_fsm,
    cloudflare_fsm,
    common,
    deploy,
    domains_fsm,
    hosts_fsm,
    ip_sets,
    managed_pool_fsm,
    member,
    monitoring,
    node_domain_fsm,
    nodes_menu,
    org_select,
    pingachock_fsm,
    remnawave_fsm,
    superadmin,
)
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.redis import RedisMiddleware
from bot.middlewares.role import RoleMiddleware
from config import settings
from db.session import async_session_factory
import services.availability_monitor as _avail_monitor
from services.availability_monitor import run_availability_monitor
from services.ip_pool_scorer import run_ip_pool_scorer
from services.monitoring_service import run_monitoring

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    bot = Bot(token=settings.bot_token)
    storage = RedisStorage.from_url(settings.redis_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    dp = Dispatcher(storage=storage)

    dp.update.outer_middleware(DbSessionMiddleware(session_factory=async_session_factory))
    dp.update.outer_middleware(RoleMiddleware(superadmin_ids=settings.superadmin_ids))
    dp.update.outer_middleware(RedisMiddleware(redis=redis))

    dp.include_router(org_select.router)
    dp.include_router(managed_pool_fsm.router)
    dp.include_router(automation_fsm.router)
    dp.include_router(ip_sets.router)
    dp.include_router(pingachock_fsm.router)
    dp.include_router(hosts_fsm.router)
    dp.include_router(domains_fsm.router)
    dp.include_router(remnawave_fsm.router)
    dp.include_router(cloudflare_fsm.router)
    dp.include_router(aws_fsm.router)
    dp.include_router(deploy.router)
    dp.include_router(nodes_menu.router)
    dp.include_router(node_domain_fsm.router)
    dp.include_router(monitoring.router)
    dp.include_router(common.router)
    dp.include_router(superadmin.router)
    dp.include_router(member.router)

    # Initialise availability monitor with DB session factory so it can
    # fire on-demand checks (e.g. immediately after group creation).
    _avail_monitor.init(async_session_factory)

    monitoring_task = asyncio.create_task(run_monitoring(bot, redis))
    availability_task = asyncio.create_task(
        run_availability_monitor(bot, async_session_factory)
    )
    pool_scorer_task = asyncio.create_task(
        run_ip_pool_scorer(async_session_factory)
    )
    try:
        await dp.start_polling(bot)
    finally:
        monitoring_task.cancel()
        availability_task.cancel()
        pool_scorer_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
        try:
            await availability_task
        except asyncio.CancelledError:
            pass
        try:
            await pool_scorer_task
        except asyncio.CancelledError:
            pass
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
