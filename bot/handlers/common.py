from __future__ import annotations
import asyncio

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import main_menu_kb, nodes_panels_kb, org_select_kb, remnawave_kb, settings_kb
from db.models.organization import Organization
from db.models.user import User
from services.organization_service import OrganizationService
from services.remnawave_api_service import get_nodes
from services.remnawave_service import RemnaWaveService

router = Router()

MENU_TEXT = "Вы в главном меню"


@router.message(CommandStart())
async def start_handler(
    message: Message,
    db_user: User,
    active_org: Organization | None,
    session: AsyncSession,
    user_orgs: list | None = None,
) -> None:
    if active_org is None:
        if user_orgs is not None:
            # Injected by middleware (multiple orgs case)
            orgs = user_orgs
        else:
            # Superadmin or fallback — fetch from DB
            org_svc = OrganizationService(session)
            orgs = await org_svc.get_user_orgs(db_user.id)

        if not orgs:
            await message.answer(
                "Нет доступных организаций. Создайте первую: /neworg <название>"
            )
            return

        await message.answer("Выберите организацию:", reply_markup=org_select_kb(orgs))
        return

    await message.answer(
        f"Организация: {active_org.name}\n\n{MENU_TEXT}",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "menu:back")
async def back_to_menu_cb(call: CallbackQuery, active_org: Organization) -> None:
    await call.answer()
    await call.message.edit_text(
        f"Организация: {active_org.name}\n\n{MENU_TEXT}",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "menu:nodes")
async def nodes_cb(call: CallbackQuery, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_org_panels(active_org.id)

    if not panels:
        await call.message.edit_text(
            "🖥 Ноды\n\nНет подключённых панелей Remnawave.",
            reply_markup=nodes_panels_kb([]),
        )
        return

    async def _panel_stats(panel) -> tuple[int, str, str]:
        try:
            nodes = await get_nodes(panel.url, panel.api_token)
            total = len(nodes)
            online = sum(1 for n in nodes if n.get("isConnected"))
            offline = total - online
            return panel.id, panel.tag, f"{online}/{offline}/{total}"
        except Exception:
            return panel.id, panel.tag, "недоступна"

    panels_stats = list(await asyncio.gather(*[_panel_stats(p) for p in panels]))
    await call.message.edit_text(
        "🖥 Ноды",
        reply_markup=nodes_panels_kb(panels_stats),
    )


@router.callback_query(F.data == "menu:settings")
async def settings_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("⚙️ Настройки", reply_markup=settings_kb())


# --- Settings: Remnawave ---

@router.callback_query(F.data == "settings:remnawave")
async def remnawave_cb(call: CallbackQuery, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_org_panels(active_org.id)

    if panels:
        lines = "\n".join(f"• {p.tag} — {p.url}" for p in panels)
        text = f"Ваши Remnawave:\n\n{lines}"
    else:
        text = "Ваши Remnawave:\n\nУ вас пока нет добавленных панелей."

    await call.message.edit_text(text, reply_markup=remnawave_kb())
