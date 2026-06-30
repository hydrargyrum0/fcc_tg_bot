import re
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    cancel_edit_kb,
    cancel_input_kb,
    manage_panels_kb,
    panel_detail_kb,
    remnawave_kb,
)
from bot.states.remnawave import AddRemnawave, EditRemnawave
from db.models.remnawave_panel import RemnaWavePanel
from db.models.user import User
from services.remnawave_service import RemnaWaveService

router = Router()


def _normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    return f"https://{parsed.netloc}"


def _panel_detail_text(panel: RemnaWavePanel) -> str:
    return (
        f"Remnawave — {panel.tag}\n\n"
        f"URL: {panel.url}\n"
        f"Статус: —\n"
        f"Последняя проверка: —"
    )


async def _show_remnawave_list(
    message: Message,
    db_user: User,
    session: AsyncSession,
    edit: bool = False,
) -> None:
    svc = RemnaWaveService(session)
    panels = await svc.get_user_panels(db_user.id)

    if panels:
        lines = "\n".join(f"• {p.tag} — {p.url}" for p in panels)
        text = f"Ваши Remnawave:\n\n{lines}"
    else:
        text = "Ваши Remnawave:\n\nУ вас пока нет добавленных панелей."

    if edit:
        await message.edit_text(text, reply_markup=remnawave_kb())
    else:
        await message.answer(text, reply_markup=remnawave_kb())


# ─────────────────────────────────────────────
#  ADD FLOW
# ─────────────────────────────────────────────

@router.callback_query(F.data == "remnawave:add")
async def remnawave_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AddRemnawave.waiting_url)
    await call.message.edit_text(
        "Сейчас вы подключаете Remnawave\n\nУкажите URL панели в любом виде:",
        reply_markup=cancel_input_kb(),
    )


@router.message(AddRemnawave.waiting_url)
async def add_got_url(message: Message, state: FSMContext) -> None:
    url = _normalize_url(message.text or "")
    await state.update_data(url=url)
    await state.set_state(AddRemnawave.waiting_token)
    await message.answer(
        "Укажите API токен для вашего Remnawave:",
        reply_markup=cancel_input_kb(),
    )


@router.message(AddRemnawave.waiting_token)
async def add_got_token(message: Message, state: FSMContext) -> None:
    await state.update_data(api_token=(message.text or "").strip())
    await state.set_state(AddRemnawave.waiting_tag)
    await message.answer(
        "Укажите ваш Тег для этого Remnawave:",
        reply_markup=cancel_input_kb(),
    )


@router.message(AddRemnawave.waiting_tag)
async def add_got_tag(
    message: Message,
    state: FSMContext,
    db_user: User,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    svc = RemnaWaveService(session)
    await svc.add_panel(
        user_id=db_user.id,
        url=data["url"],
        api_token=data["api_token"],
        tag=(message.text or "").strip(),
    )
    await state.clear()
    await _show_remnawave_list(message, db_user, session, edit=False)


@router.callback_query(F.data == "remnawave:add_cancel")
async def add_cancel(
    call: CallbackQuery,
    state: FSMContext,
    db_user: User,
    session: AsyncSession,
) -> None:
    await call.answer()
    await state.clear()
    await _show_remnawave_list(call.message, db_user, session, edit=True)


# ─────────────────────────────────────────────
#  MANAGE FLOW
# ─────────────────────────────────────────────

@router.callback_query(F.data == "remnawave:manage")
async def manage_list(
    call: CallbackQuery,
    db_user: User,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_user_panels(db_user.id)

    if not panels:
        await call.message.edit_text(
            "Ваши Remnawave:\n\nУ вас нет добавленных панелей.",
            reply_markup=remnawave_kb(),
        )
        return

    lines = "\n".join(f"• {p.tag} — {p.url}" for p in panels)
    await call.message.edit_text(
        f"Ваши Remnawave:\n\n{lines}",
        reply_markup=manage_panels_kb(panels),
    )


@router.callback_query(F.data.regexp(r"^rwp:\d+$"))
async def show_panel_detail(
    call: CallbackQuery,
    db_user: User,
    session: AsyncSession,
) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[1])
    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, db_user.id)
    if not panel:
        await call.answer("Панель не найдена.", show_alert=True)
        return
    await call.message.edit_text(
        _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel.id),
    )


@router.callback_query(F.data == "rwp:back")
async def back_to_manage_list(
    call: CallbackQuery,
    db_user: User,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_user_panels(db_user.id)
    lines = "\n".join(f"• {p.tag} — {p.url}" for p in panels)
    await call.message.edit_text(
        f"Ваши Remnawave:\n\n{lines}",
        reply_markup=manage_panels_kb(panels),
    )


# ─────────────────────────────────────────────
#  EDIT FLOW
# ─────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^rwp:eurl:\d+$"))
async def edit_url_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])
    await state.set_state(EditRemnawave.waiting_url)
    await state.update_data(panel_id=panel_id)
    await call.message.edit_text(
        "Введите новый URL панели:",
        reply_markup=cancel_edit_kb(),
    )


@router.callback_query(F.data.regexp(r"^rwp:etag:\d+$"))
async def edit_tag_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])
    await state.set_state(EditRemnawave.waiting_tag)
    await state.update_data(panel_id=panel_id)
    await call.message.edit_text(
        "Введите новый Тег для этой панели:",
        reply_markup=cancel_edit_kb(),
    )


@router.callback_query(F.data.regexp(r"^rwp:etok:\d+$"))
async def edit_token_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])
    await state.set_state(EditRemnawave.waiting_token)
    await state.update_data(panel_id=panel_id)
    await call.message.edit_text(
        "Введите новый API токен:",
        reply_markup=cancel_edit_kb(),
    )


@router.message(EditRemnawave.waiting_url)
async def edit_got_url(
    message: Message,
    state: FSMContext,
    db_user: User,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    url = _normalize_url(message.text or "")
    svc = RemnaWaveService(session)
    await svc.update_url(panel_id, db_user.id, url)
    panel = await svc.get_panel_by_id(panel_id, db_user.id)
    await state.clear()
    await message.answer(
        _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel_id),
    )


@router.message(EditRemnawave.waiting_tag)
async def edit_got_tag(
    message: Message,
    state: FSMContext,
    db_user: User,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    tag = (message.text or "").strip()
    svc = RemnaWaveService(session)
    await svc.update_tag(panel_id, db_user.id, tag)
    panel = await svc.get_panel_by_id(panel_id, db_user.id)
    await state.clear()
    await message.answer(
        _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel_id),
    )


@router.message(EditRemnawave.waiting_token)
async def edit_got_token(
    message: Message,
    state: FSMContext,
    db_user: User,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    svc = RemnaWaveService(session)
    await svc.update_token(panel_id, db_user.id, (message.text or "").strip())
    panel = await svc.get_panel_by_id(panel_id, db_user.id)
    await state.clear()
    await message.answer(
        _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel_id),
    )


@router.callback_query(F.data == "rwp:cancel_edit")
async def cancel_edit(
    call: CallbackQuery,
    state: FSMContext,
    db_user: User,
    session: AsyncSession,
) -> None:
    await call.answer()
    data = await state.get_data()
    panel_id: int = data.get("panel_id")
    await state.clear()

    if panel_id:
        svc = RemnaWaveService(session)
        panel = await svc.get_panel_by_id(panel_id, db_user.id)
        if panel:
            await call.message.edit_text(
                _panel_detail_text(panel),
                reply_markup=panel_detail_kb(panel_id),
            )
            return

    await _show_remnawave_list(call.message, db_user, session, edit=True)
