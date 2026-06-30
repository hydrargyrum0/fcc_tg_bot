import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from bot.handlers import common, superadmin, member
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.role import RoleMiddleware
from config import settings
from db.session import async_session_factory

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    bot = Bot(token=settings.bot_token)
    storage = RedisStorage.from_url(settings.redis_url)
    dp = Dispatcher(storage=storage)

    dp.update.outer_middleware(DbSessionMiddleware(session_factory=async_session_factory))
    dp.update.outer_middleware(RoleMiddleware(superadmin_ids=settings.superadmin_ids))

    dp.include_router(common.router)
    dp.include_router(superadmin.router)
    dp.include_router(member.router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
