from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.user import UserRole
from services.user_service import UserService


class RoleMiddleware(BaseMiddleware):
    def __init__(self, superadmin_ids: list[int]) -> None:
        self.superadmin_ids = superadmin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if not tg_user:
            return await handler(event, data)

        session: AsyncSession = data["session"]
        role = UserRole.superadmin if tg_user.id in self.superadmin_ids else UserRole.member
        svc = UserService(session)
        user, _ = await svc.get_or_create(
            user_id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            role=role,
        )
        data["db_user"] = user
        return await handler(event, data)
