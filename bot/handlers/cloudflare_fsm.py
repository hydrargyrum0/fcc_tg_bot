from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import cancel_cf_kb, cloudflare_kb
from bot.states.cloudflare import EditCloudflare
from db.models.organization import Organization
from db.models.user import User
from services.audit_service import send_audit
from services.cloudflare_service import CloudflareService

router = Router()

_TEXT = "Вы настраиваете API Cloudflare:"


async def _show_cloudflare(
    message: Message,
    active_org: Organization,
    session: AsyncSession,
    edit: bool = False,
) -> None:
    svc = CloudflareService(session)
    settings = await svc.get_or_create(active_org.id)
    kb = cloudflare_kb(settings.email, bool(settings.api_key))
    if edit:
        await message.edit_text(_TEXT, reply_markup=kb)
    else:
        await message.answer(_TEXT, reply_markup=kb)


@router.callback_query(F.data == "settings:cloudflare")
async def cloudflare_cb(call: CallbackQuery, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    await _show_cloudflare(call.message, active_org, session, edit=True)


@router.callback_query(F.data == "cf:edit_email")
async def edit_email_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(EditCloudflare.waiting_email)
    await call.message.edit_text("Введите email Cloudflare:", reply_markup=cancel_cf_kb())


@router.callback_query(F.data == "cf:edit_token")
async def edit_token_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(EditCloudflare.waiting_api_key)
    await call.message.edit_text(
        "Введите Cloudflare Global API Key:",
        reply_markup=cancel_cf_kb(),
    )


@router.message(EditCloudflare.waiting_email)
async def got_email(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
    db_user: User,
) -> None:
    email = (message.text or "").strip()
    svc = CloudflareService(session)
    await svc.update_email(active_org.id, email)
    await state.clear()
    send_audit(message.bot, active_org.id, db_user, f"Обновил Cloudflare email: {email}")
    await _show_cloudflare(message, active_org, session, edit=False)


@router.message(EditCloudflare.waiting_api_key)
async def got_api_key(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
    db_user: User,
) -> None:
    svc = CloudflareService(session)
    await svc.update_api_key(active_org.id, (message.text or "").strip())
    await state.clear()
    send_audit(message.bot, active_org.id, db_user, "Обновил Cloudflare API Key")
    await _show_cloudflare(message, active_org, session, edit=False)


@router.callback_query(F.data == "cf:cancel")
async def cancel_edit(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    await state.clear()
    await _show_cloudflare(call.message, active_org, session, edit=True)
