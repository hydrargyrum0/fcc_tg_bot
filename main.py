import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from bot.handlers import (
    aws_fsm,
    cloudflare_fsm,
    common,
    deploy,
    member,
    monitoring,
    node_domain_fsm,
    nodes_menu,
    org_select,
    remnawave_fsm,
    superadmin,
)
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.redis import RedisMiddleware
from bot.middlewares.role import RoleMiddleware
from config import settings
from db.session import async_session_factory
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

    monitoring_task = asyncio.create_task(run_monitoring(bot, redis))
    try:
        await dp.start_polling(bot)
    finally:
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
