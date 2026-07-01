from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    back_to_node_kb,
    nodes_node_detail_kb,
    nodes_panel_detail_kb,
    nodes_restart_confirm_kb,
    nodes_restart_type_kb,
)
from db.models.organization import Organization
from services.remnawave_api_service import (
    RemnaWaveAPIError,
    disable_node,
    enable_node,
    get_nodes,
    restart_node,
)
from services.remnawave_service import RemnaWaveService

router = Router()


def _node_status(node: dict) -> str:
    if node.get("isDisabled"):
        return "⛔"
    if node.get("isConnecting"):
        return "🟡"
    if node.get("isConnected"):
        return "🟢"
    return "🔴"


def _node_addr(node: dict) -> str:
    addr = node.get("address") or "—"
    port = node.get("port")
    return f"{addr}:{port}" if port else addr


async def _get_panel_and_node(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
    panel_id: int,
    node_uuid: str,
) -> tuple | None:
    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await call.answer("Панель не найдена.", show_alert=True)
        return None
    try:
        nodes = await get_nodes(panel.url, panel.api_token)
    except RemnaWaveAPIError:
        await call.answer("Не удалось загрузить данные.", show_alert=True)
        return None
    node = next((n for n in nodes if n["uuid"] == node_uuid), None)
    if not node:
        await call.answer("Нода не найдена.", show_alert=True)
        return None
    return panel, node


def _node_detail_text(node: dict) -> str:
    status = _node_status(node)
    versions = node.get("versions") or {}
    xray_v = versions.get("xray") or "—"
    node_v = versions.get("node") or "—"
    last_msg = node.get("lastStatusMessage") or "—"
    country = node.get("countryCode") or "—"
    users_online = node.get("usersOnline", 0)
    return (
        f"{status} {node['name']}\n\n"
        f"Адрес: {_node_addr(node)}\n"
        f"Страна: {country}\n"
        f"Пользователей онлайн: {users_online}\n"
        f"Xray: {xray_v}\n"
        f"Node: {node_v}\n"
        f"Последнее сообщение: {last_msg}"
    )


@router.callback_query(F.data.regexp(r"^nodes:panel:\d+$"))
async def panel_nodes_cb(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])

    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await call.answer("Панель не найдена.", show_alert=True)
        return

    try:
        nodes = await get_nodes(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await call.message.edit_text(
            f"❌ Не удалось загрузить ноды панели {panel.tag}:\n{e}",
            reply_markup=nodes_panel_detail_kb(panel_id, []),
        )
        return

    if not nodes:
        await call.message.edit_text(
            f"Ноды панели {panel.tag}:\n\nНод нет.",
            reply_markup=nodes_panel_detail_kb(panel_id, []),
        )
        return

    lines = [
        f"{_node_status(n)} {n['name']} ({_node_addr(n)})"
        for n in nodes
    ]
    text = f"Ноды панели {panel.tag} ({len(nodes)}):\n\n" + "\n".join(lines)
    await call.message.edit_text(
        text,
        reply_markup=nodes_panel_detail_kb(panel_id, nodes),
    )


@router.callback_query(F.data.regexp(r"^nodes:node:\d+:.+$"))
async def node_detail_cb(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    parts = call.data.split(":", 3)
    panel_id = int(parts[2])
    node_uuid = parts[3]

    result = await _get_panel_and_node(call, active_org, session, panel_id, node_uuid)
    if result is None:
        return
    panel, node = result

    await call.message.edit_text(
        _node_detail_text(node),
        reply_markup=nodes_node_detail_kb(panel_id, node_uuid, is_disabled=node.get("isDisabled", False)),
    )


@router.callback_query(F.data.regexp(r"^nodes:restart:\d+:.+$"))
async def node_restart_cb(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    parts = call.data.split(":", 3)
    panel_id = int(parts[2])
    node_uuid = parts[3]

    result = await _get_panel_and_node(call, active_org, session, panel_id, node_uuid)
    if result is None:
        return
    _, node = result

    await call.message.edit_text(
        f"🔄 Перезапуск ноды {node['name']}\n\nВыберите тип перезапуска:",
        reply_markup=nodes_restart_type_kb(panel_id, node_uuid),
    )


@router.callback_query(F.data.regexp(r"^nodes:restart_type:\d+:.+:[01]$"))
async def node_restart_type_cb(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    parts = call.data.split(":", 4)
    panel_id = int(parts[2])
    node_uuid = parts[3]
    force = parts[4]

    result = await _get_panel_and_node(call, active_org, session, panel_id, node_uuid)
    if result is None:
        return
    _, node = result

    mode_label = "принудительно ⚡" if force == "1" else "плавно 🔄"
    await call.message.edit_text(
        f"Вы уверены, что хотите перезапустить ноду {node['name']} {mode_label}?",
        reply_markup=nodes_restart_confirm_kb(panel_id, node_uuid, force),
    )


@router.callback_query(F.data.regexp(r"^nodes:restart_conf:\d+:.+:[01]$"))
async def node_restart_confirm_cb(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    parts = call.data.split(":", 4)
    panel_id = int(parts[2])
    node_uuid = parts[3]
    force = parts[4]

    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await call.answer("Панель не найдена.", show_alert=True)
        return

    force_bool = force == "1"
    mode_label = "принудительного" if force_bool else "плавного"

    try:
        sent = await restart_node(panel.url, panel.api_token, node_uuid, force_bool)
    except RemnaWaveAPIError as e:
        await call.message.edit_text(
            f"❌ Ошибка при перезапуске:\n{e}",
            reply_markup=back_to_node_kb(panel_id, node_uuid),
        )
        return

    if sent:
        text = f"✅ Команда {mode_label} перезапуска отправлена."
    else:
        text = f"⚠️ Команда {mode_label} перезапуска не была отправлена (eventSent=false)."

    await call.message.edit_text(
        text,
        reply_markup=back_to_node_kb(panel_id, node_uuid),
    )


@router.callback_query(F.data.regexp(r"^nodes:toggle:\d+:.+$"))
async def node_toggle_cb(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    parts = call.data.split(":", 3)
    panel_id = int(parts[2])
    node_uuid = parts[3]

    result = await _get_panel_and_node(call, active_org, session, panel_id, node_uuid)
    if result is None:
        return
    panel, node = result

    is_disabled = node.get("isDisabled", False)
    action_label = "включить" if is_disabled else "выключить"

    try:
        if is_disabled:
            await enable_node(panel.url, panel.api_token, node_uuid)
        else:
            await disable_node(panel.url, panel.api_token, node_uuid)
    except RemnaWaveAPIError as e:
        await call.answer(f"❌ Не удалось {action_label} ноду: {e}", show_alert=True)
        return

    try:
        nodes = await get_nodes(panel.url, panel.api_token)
    except RemnaWaveAPIError:
        await call.answer("✅ Готово", show_alert=False)
        return

    updated = next((n for n in nodes if n["uuid"] == node_uuid), node)
    await call.message.edit_text(
        _node_detail_text(updated),
        reply_markup=nodes_node_detail_kb(panel_id, node_uuid, is_disabled=updated.get("isDisabled", False)),
    )


@router.callback_query(F.data == "nodes:wip")
async def nodes_wip_cb(call: CallbackQuery) -> None:
    await call.answer("Функция в разработке 🔧", show_alert=True)
