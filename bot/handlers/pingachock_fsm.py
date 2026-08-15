"""Pingachock settings handler — connect / edit / test / delete / node selection."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    pc_nodes_kb,
    pingachock_cancel_kb,
    pingachock_configured_kb,
    pingachock_not_configured_kb,
)
from bot.states.pingachock import PingachockFSM
from db.models.organization import Organization
from db.models.user import User
from services.audit_service import send_audit
from services.pingachock_api_service import PingachockAPIError, get_nodes
from services.pingachock_service import PingachockService

router = Router()


# ── helpers ───────────────────────────────────────────────────────────────────

def _mask_key(key: str) -> str:
    """Show first 8 chars then asterisks."""
    return key[:8] + "****" if len(key) > 8 else "****"


async def _show_menu(
    target: CallbackQuery | Message,
    session: AsyncSession,
    active_org: Organization,
    *,
    edit: bool = True,
) -> None:
    svc = PingachockService(session)
    settings = await svc.get_settings(active_org.id)
    msg = target.message if isinstance(target, CallbackQuery) else target

    if not settings:
        text = (
            "🔍 <b>Pingachock</b>\n\n"
            "Не подключён. Pingachock — распределённый сервис проверки доступности "
            "IP/доменов из Туркменистана.\n\n"
            "Нажмите «Подключить» чтобы ввести URL и API-ключ."
        )
        kb = pingachock_not_configured_kb()
    else:
        # Fetch node count for status display (non-blocking, silent on error)
        online = total = 0
        status_line = ""
        try:
            nodes = await get_nodes(settings.api_url, settings.api_key)
            total = len(nodes)
            online = sum(1 for n in nodes if n.get("online"))
            status_line = f"\n🟢 Онлайн-узлов: {online}/{total}"
        except PingachockAPIError as e:
            status_line = f"\n🔴 Нет связи: {str(e)[:80]}"

        text = (
            f"🔍 <b>Pingachock</b>\n\n"
            f"🌐 URL: <code>{settings.api_url}</code>\n"
            f"🔑 Ключ: <code>{_mask_key(settings.api_key)}</code>"
            f"{status_line}"
        )
        kb = pingachock_configured_kb()

    if edit:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ── entry ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "settings:pingachock")
async def pingachock_menu(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    await _show_menu(call, session, active_org, edit=True)


# ── connect: step 1 — URL ─────────────────────────────────────────────────────

@router.callback_query(F.data == "pc:connect")
async def connect_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(PingachockFSM.waiting_url)
    await call.message.edit_text(
        "🔌 <b>Подключение Pingachock</b>\n\n"
        "Введите URL сервера (без завершающего слэша):\n"
        "<code>https://pingachock.rapeer.com:30031</code>",
        reply_markup=pingachock_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(PingachockFSM.waiting_url)
async def got_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip().rstrip("/")
    if not url.startswith("http"):
        await message.answer(
            "❌ URL должен начинаться с <code>http://</code> или <code>https://</code>",
            reply_markup=pingachock_cancel_kb(),
            parse_mode="HTML",
        )
        return
    await state.update_data(api_url=url)
    await state.set_state(PingachockFSM.waiting_key)
    await message.answer(
        f"URL: <code>{url}</code>\n\nТеперь введите API-ключ:",
        reply_markup=pingachock_cancel_kb(),
        parse_mode="HTML",
    )


# ── connect: step 2 — API key → test → save ───────────────────────────────────

@router.message(PingachockFSM.waiting_key)
async def got_key(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
    db_user: User,
) -> None:
    key = (message.text or "").strip()
    if not key:
        await message.answer("❌ API-ключ не может быть пустым:", reply_markup=pingachock_cancel_kb())
        return

    data = await state.get_data()
    api_url: str = data["api_url"]

    status_msg = await message.answer("⏳ Проверяю соединение с Pingachock...")

    try:
        nodes = await get_nodes(api_url, key)
    except PingachockAPIError as e:
        await status_msg.edit_text(
            f"❌ Не удалось подключиться:\n<code>{e}</code>\n\n"
            "Проверьте URL и ключ и попробуйте снова.",
            reply_markup=pingachock_cancel_kb(),
            parse_mode="HTML",
        )
        return

    # Save to DB
    svc = PingachockService(session)
    await svc.save_settings(active_org.id, api_url, key)
    await state.clear()

    online = sum(1 for n in nodes if n.get("online"))
    total = len(nodes)
    send_audit(message.bot, active_org.id, db_user, f"Подключил Pingachock: {api_url}")
    await status_msg.edit_text(
        f"✅ <b>Pingachock подключён!</b>\n\n"
        f"🌐 <code>{api_url}</code>\n"
        f"🟢 Узлов онлайн: {online}/{total}",
        parse_mode="HTML",
    )


# ── edit URL ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "pc:edit_url")
async def edit_url_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(PingachockFSM.waiting_url)
    await call.message.edit_text(
        "Введите новый URL сервера Pingachock:",
        reply_markup=pingachock_cancel_kb(),
    )


# ── edit key ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "pc:edit_key")
async def edit_key_start(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = PingachockService(session)
    settings = await svc.get_settings(active_org.id)
    if not settings:
        await call.answer("Настройки не найдены.", show_alert=True)
        return
    # Put current URL in state so got_key reuses it
    await state.update_data(api_url=settings.api_url)
    await state.set_state(PingachockFSM.waiting_key)
    await call.message.edit_text(
        "Введите новый API-ключ Pingachock:",
        reply_markup=pingachock_cancel_kb(),
    )


# ── test connection ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "pc:test")
async def test_connection(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = PingachockService(session)
    settings = await svc.get_settings(active_org.id)
    if not settings:
        await call.answer("Настройки не найдены.", show_alert=True)
        return

    await call.message.edit_text("⏳ Проверяю соединение...")
    try:
        nodes = await get_nodes(settings.api_url, settings.api_key)
        online = sum(1 for n in nodes if n.get("online"))
        total = len(nodes)
        await call.answer(f"✅ OK — {online}/{total} узлов онлайн", show_alert=True)
    except PingachockAPIError as e:
        await call.answer(f"❌ Ошибка: {str(e)[:200]}", show_alert=True)

    # Restore menu
    await _show_menu(call, session, active_org, edit=True)


# ── delete ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "pc:delete")
async def delete_settings(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
    db_user: User,
) -> None:
    await call.answer()
    svc = PingachockService(session)
    await svc.delete_settings(active_org.id)
    send_audit(call.bot, active_org.id, db_user, "Удалил настройки Pingachock")
    await _show_menu(call, session, active_org, edit=True)


# ── cancel ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "pc:cancel")
async def cancel(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    await state.clear()
    await _show_menu(call, session, active_org, edit=True)


# ── node selection ─────────────────────────────────────────────────────────────

def _nodes_text(selected_ids: list[str]) -> str:
    if selected_ids:
        return (
            "📡 <b>Узлы сканирования</b>\n\n"
            f"Выбрано узлов: <b>{len(selected_ids)}</b>.\n"
            "Только они будут использоваться при проверках.\n\n"
            "Нажмите на узел чтобы включить/отключить, затем «💾 Сохранить»."
        )
    return (
        "📡 <b>Узлы сканирования</b>\n\n"
        "Фильтр не задан — используются <b>все доступные узлы</b>.\n\n"
        "Нажмите на узел чтобы включить его в фильтр, затем «💾 Сохранить»."
    )


@router.callback_query(F.data == "pc:nodes")
async def nodes_menu(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = PingachockService(session)
    settings = await svc.get_settings(active_org.id)
    if not settings:
        await call.answer("Pingachock не настроен.", show_alert=True)
        return

    await call.message.edit_text("⏳ Загружаю список узлов...")
    try:
        nodes = await get_nodes(settings.api_url, settings.api_key)
    except PingachockAPIError as e:
        await call.answer(f"❌ Не удалось получить узлы: {str(e)[:200]}", show_alert=True)
        await _show_menu(call, session, active_org, edit=True)
        return

    # Exclude virtual nodes ("server") from the selection UI
    nodes = [n for n in nodes if not n.get("is_virtual", False)]

    current_ids: list[str] = list(settings.node_ids or [])
    await state.set_state(PingachockFSM.selecting_nodes)
    await state.update_data(nodes=nodes, selected_ids=current_ids)

    await call.message.edit_text(
        _nodes_text(current_ids),
        reply_markup=pc_nodes_kb(nodes, set(current_ids)),
        parse_mode="HTML",
    )


@router.callback_query(PingachockFSM.selecting_nodes, F.data.startswith("pc:node_toggle:"))
async def node_toggle(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    node_id = call.data[len("pc:node_toggle:"):]
    data = await state.get_data()
    nodes: list[dict] = data.get("nodes", [])
    selected: list[str] = list(data.get("selected_ids", []))

    if node_id in selected:
        selected.remove(node_id)
    else:
        selected.append(node_id)

    await state.update_data(selected_ids=selected)
    await call.message.edit_text(
        _nodes_text(selected),
        reply_markup=pc_nodes_kb(nodes, set(selected)),
        parse_mode="HTML",
    )


@router.callback_query(PingachockFSM.selecting_nodes, F.data == "pc:nodes_all")
async def nodes_use_all(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    """Clear the node filter — use all nodes."""
    await call.answer()
    svc = PingachockService(session)
    await svc.update_node_ids(active_org.id, None)
    await state.clear()
    await call.message.edit_text(
        "✅ Фильтр узлов снят — будут использоваться <b>все доступные узлы</b>.",
        parse_mode="HTML",
    )
    await _show_menu(call, session, active_org, edit=False)


@router.callback_query(PingachockFSM.selecting_nodes, F.data == "pc:nodes_save")
async def nodes_save(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    data = await state.get_data()
    selected: list[str] = data.get("selected_ids", [])

    svc = PingachockService(session)
    await svc.update_node_ids(active_org.id, selected if selected else None)
    await state.clear()

    if selected:
        await call.message.edit_text(
            f"✅ Сохранено. Активных узлов: <b>{len(selected)}</b>.",
            parse_mode="HTML",
        )
    else:
        await call.message.edit_text(
            "✅ Фильтр снят — будут использоваться <b>все доступные узлы</b>.",
            parse_mode="HTML",
        )
    await _show_menu(call, session, active_org, edit=False)


@router.callback_query(PingachockFSM.selecting_nodes, F.data == "pc:nodes_back")
async def nodes_back(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    """Discard unsaved node changes and return to Pingachock menu."""
    await call.answer()
    await state.clear()
    await _show_menu(call, session, active_org, edit=True)
