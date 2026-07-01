from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import main_menu_kb, org_select_kb
from db.models.user import User
from services.organization_service import OrganizationService
from services.user_service import UserService

router = Router()

MENU_TEXT = "Вы в главном меню"


@router.callback_query(F.data.regexp(r"^org:select:\d+$"))
async def org_select_cb(
    call: CallbackQuery,
    db_user: User,
    session: AsyncSession,
) -> None:
    await call.answer()
    org_id = int(call.data.split(":")[2])

    org_svc = OrganizationService(session)
    if not await org_svc.is_member(org_id, db_user.id):
        await call.answer("Нет доступа к этой организации.", show_alert=True)
        return

    user_svc = UserService(session)
    await user_svc.set_active_org(db_user.id, org_id)

    org = await org_svc.get_org_by_id(org_id)
    await call.message.edit_text(
        f"Организация: {org.name}\n\n{MENU_TEXT}",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "org:switch")
async def org_switch_cb(
    call: CallbackQuery,
    db_user: User,
    session: AsyncSession,
) -> None:
    await call.answer()
    org_svc = OrganizationService(session)
    user_orgs = await org_svc.get_user_orgs(db_user.id)

    if not user_orgs:
        await call.answer("У вас нет доступных организаций.", show_alert=True)
        return

    await call.message.edit_text(
        "Выберите организацию:",
        reply_markup=org_select_kb(user_orgs),
    )
