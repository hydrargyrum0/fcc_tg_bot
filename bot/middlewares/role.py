from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.user import UserRole
from services.organization_service import OrganizationService
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

        user_svc = UserService(session)
        db_user, _ = await user_svc.get_or_create(
            user_id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            role=role,
        )
        data["db_user"] = db_user

        org_svc = OrganizationService(session)

        if db_user.role == UserRole.superadmin:
            active_org = None
            if db_user.active_org_id:
                active_org = await org_svc.get_org_by_id(db_user.active_org_id)
            data["active_org"] = active_org
            return await handler(event, data)

        # Regular user — must be in an org
        user_orgs = await org_svc.get_user_orgs(tg_user.id)

        if not user_orgs:
            if not db_user.notified_no_access:
                await user_svc.mark_notified_no_access(tg_user.id)
                bot = data.get("bot")
                if bot:
                    try:
                        await bot.send_message(
                            tg_user.id,
                            "У вас нет доступа к этому боту. Обратитесь к администратору.",
                        )
                    except Exception:
                        pass
            return

        # Validate active org
        active_org = None
        if db_user.active_org_id:
            if await org_svc.is_member(db_user.active_org_id, tg_user.id):
                active_org = await org_svc.get_org_by_id(db_user.active_org_id)
            else:
                await user_svc.set_active_org(tg_user.id, None)

        # Auto-select when only one org
        if active_org is None and len(user_orgs) == 1:
            active_org = user_orgs[0]
            await user_svc.set_active_org(tg_user.id, active_org.id)

        if active_org is None:
            # Multiple orgs, none selected — only allow /start and org selection
            update: Update = event  # type: ignore[assignment]
            is_start = bool(
                update.message
                and update.message.text
                and update.message.text.strip().startswith("/start")
            )
            is_org_select = bool(
                update.callback_query
                and (update.callback_query.data or "").startswith("org:select:")
            )
            if not is_start and not is_org_select:
                return
            data["active_org"] = None
            data["user_orgs"] = user_orgs
            return await handler(event, data)

        data["active_org"] = active_org
        return await handler(event, data)
