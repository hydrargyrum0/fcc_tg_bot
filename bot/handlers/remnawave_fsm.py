import re
from datetime import datetime, timezone
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
from db.models.organization import Organization
from db.models.remnawave_panel import RemnaWavePanel
from db.models.user import User
from services.audit_service import send_audit
from services.remnawave_api_service import check_panel_online
from services.remnawave_service import RemnaWaveService

router = Router()


def _normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    return f"https://{parsed.netloc}"


async def _panel_detail_text(panel: RemnaWavePanel) -> str:
    is_online = await check_panel_online(panel.url, panel.api_token)
    status = "🟢 Онлайн" if is_online else "🔴 Офлайн"
    checked_at = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S")
    return (
        f"Remnawave — {panel.tag}\n\n"
        f"URL: {panel.url}\n"
        f"Node Secret: *****\n"
        f"Node Port: {panel.node_port}\n"
        f"Статус: {status}\n"
        f"Последняя проверка: {checked_at}"
    )


async def _show_remnawave_list(
    message: Message,
    active_org: Organization,
    session: AsyncSession,
    edit: bool = False,
) -> None:
    svc = RemnaWaveService(session)
    panels = await svc.get_org_panels(active_org.id)

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
    await state.set_state(AddRemnawave.waiting_node_secret)
    await message.answer(
        "Укажите Secret Key для нод этого Remnawave:",
        reply_markup=cancel_input_kb(),
    )


@router.message(AddRemnawave.waiting_node_secret)
async def add_got_node_secret(message: Message, state: FSMContext) -> None:
    await state.update_data(node_secret_key=(message.text or "").strip())
    await state.set_state(AddRemnawave.waiting_node_port)
    await message.answer(
        "Укажите порт для нод этого Remnawave:",
        reply_markup=cancel_input_kb(),
    )


@router.message(AddRemnawave.waiting_node_port)
async def add_got_node_port(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Порт должен быть числом. Попробуйте ещё раз:", reply_markup=cancel_input_kb())
        return
    await state.update_data(node_port=int(text))
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
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    tag = (message.text or "").strip()
    svc = RemnaWaveService(session)
    await svc.add_panel(
        org_id=active_org.id,
        url=data["url"],
        api_token=data["api_token"],
        node_secret_key=data["node_secret_key"],
        node_port=data["node_port"],
        tag=tag,
    )
    await state.clear()
    send_audit(message.bot, active_org.id, db_user, f"Добавил панель Remnawave: {tag}")
    await _show_remnawave_list(message, active_org, session, edit=False)


@router.callback_query(F.data == "remnawave:add_cancel")
async def add_cancel(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    await state.clear()
    await _show_remnawave_list(call.message, active_org, session, edit=True)


# ─────────────────────────────────────────────
#  MANAGE FLOW
# ─────────────────────────────────────────────

@router.callback_query(F.data == "remnawave:manage")
async def manage_list(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_org_panels(active_org.id)

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
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[1])
    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await call.answer("Панель не найдена.", show_alert=True)
        return
    await call.message.edit_text(
        await _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel.id, panel.monitoring_enabled),
    )


@router.callback_query(F.data == "rwp:back")
async def back_to_manage_list(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_org_panels(active_org.id)
    lines = "\n".join(f"• {p.tag} — {p.url}" for p in panels)
    await call.message.edit_text(
        f"Ваши Remnawave:\n\n{lines}",
        reply_markup=manage_panels_kb(panels),
    )


@router.callback_query(F.data.regexp(r"^rwp:delete:\d+$"))
async def delete_panel(
    call: CallbackQuery,
    db_user: User,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])
    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    tag = panel.tag if panel else str(panel_id)
    await svc.delete_panel(panel_id, active_org.id)
    send_audit(call.bot, active_org.id, db_user, f"Удалил панель Remnawave: {tag}")
    await _show_remnawave_list(call.message, active_org, session, edit=True)


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


@router.callback_query(F.data.regexp(r"^rwp:ensec:\d+$"))
async def edit_node_secret_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])
    await state.set_state(EditRemnawave.waiting_node_secret)
    await state.update_data(panel_id=panel_id)
    await call.message.edit_text(
        "Введите новый Node Secret Key:",
        reply_markup=cancel_edit_kb(),
    )


@router.callback_query(F.data.regexp(r"^rwp:enport:\d+$"))
async def edit_node_port_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])
    await state.set_state(EditRemnawave.waiting_node_port)
    await state.update_data(panel_id=panel_id)
    await call.message.edit_text(
        "Введите новый Node Port:",
        reply_markup=cancel_edit_kb(),
    )


@router.message(EditRemnawave.waiting_url)
async def edit_got_url(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    url = _normalize_url(message.text or "")
    svc = RemnaWaveService(session)
    await svc.update_url(panel_id, active_org.id, url)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    await state.clear()
    await message.answer(
        await _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel.id, panel.monitoring_enabled),
    )


@router.message(EditRemnawave.waiting_tag)
async def edit_got_tag(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    tag = (message.text or "").strip()
    svc = RemnaWaveService(session)
    await svc.update_tag(panel_id, active_org.id, tag)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    await state.clear()
    await message.answer(
        await _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel.id, panel.monitoring_enabled),
    )


@router.message(EditRemnawave.waiting_token)
async def edit_got_token(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    svc = RemnaWaveService(session)
    await svc.update_token(panel_id, active_org.id, (message.text or "").strip())
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    await state.clear()
    await message.answer(
        await _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel.id, panel.monitoring_enabled),
    )


@router.message(EditRemnawave.waiting_node_secret)
async def edit_got_node_secret(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    svc = RemnaWaveService(session)
    await svc.update_node_secret(panel_id, active_org.id, (message.text or "").strip())
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    await state.clear()
    await message.answer(
        await _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel.id, panel.monitoring_enabled),
    )


@router.message(EditRemnawave.waiting_node_port)
async def edit_got_node_port(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Порт должен быть числом. Попробуйте ещё раз:", reply_markup=cancel_edit_kb())
        return
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    svc = RemnaWaveService(session)
    await svc.update_node_port(panel_id, active_org.id, int(text))
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    await state.clear()
    await message.answer(
        await _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel.id, panel.monitoring_enabled),
    )


@router.callback_query(F.data.regexp(r"^rwp:monitoring:\d+$"))
async def toggle_monitoring(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])
    svc = RemnaWaveService(session)
    new_state = await svc.toggle_monitoring(panel_id, active_org.id)
    if new_state is None:
        await call.answer("Панель не найдена.", show_alert=True)
        return
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    await call.message.edit_text(
        await _panel_detail_text(panel),
        reply_markup=panel_detail_kb(panel.id, panel.monitoring_enabled),
    )


@router.callback_query(F.data == "rwp:cancel_edit")
async def cancel_edit(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    data = await state.get_data()
    panel_id: int = data.get("panel_id")
    await state.clear()

    if panel_id:
        svc = RemnaWaveService(session)
        panel = await svc.get_panel_by_id(panel_id, active_org.id)
        if panel:
            await call.message.edit_text(
                await _panel_detail_text(panel),
                reply_markup=panel_detail_kb(panel.id, panel.monitoring_enabled),
            )
            return

    await _show_remnawave_list(call.message, active_org, session, edit=True)
