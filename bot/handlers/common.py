from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import main_menu_kb, remnawave_kb, settings_kb
from db.models.user import User
from services.remnawave_service import RemnaWaveService

router = Router()

MENU_TEXT = "Вы в главном меню"


@router.message(CommandStart())
async def start_handler(message: Message, db_user: User) -> None:
    await message.answer(MENU_TEXT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu:back")
async def back_to_menu_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(MENU_TEXT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu:deploy")
async def deploy_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("🚀 Развёртывание — в разработке", reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu:panels")
async def panels_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("🖥 Панели — в разработке", reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu:settings")
async def settings_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("⚙️ Настройки", reply_markup=settings_kb())


@router.callback_query(F.data == "menu:domains")
async def domains_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("🌐 Домены — в разработке", reply_markup=main_menu_kb())


# --- Settings: Remnawave ---

@router.callback_query(F.data == "settings:remnawave")
async def remnawave_cb(call: CallbackQuery, db_user: User, session: AsyncSession) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_user_panels(db_user.id)

    if panels:
        lines = "\n".join(f"• {p.tag} — {p.url}" for p in panels)
        text = f"Ваши Remnawave:\n\n{lines}"
    else:
        text = "Ваши Remnawave:\n\nУ вас пока нет добавленных панелей."

    await call.message.edit_text(text, reply_markup=remnawave_kb())


# --- Settings: Cloudflare / AmazonWS ---

@router.callback_query(F.data == "settings:cloudflare")
async def cloudflare_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("Cloudflare — в разработке", reply_markup=settings_kb())


@router.callback_query(F.data == "settings:amazonws")
async def amazonws_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("AmazonWS — в разработке", reply_markup=settings_kb())
