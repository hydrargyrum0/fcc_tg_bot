import math
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    domains_cancel_kb,
    domains_delete_confirm_kb,
    domains_menu_kb,
    domains_record_detail_kb,
    domains_records_kb,
    domains_zones_kb,
)
from bot.states.domains import DomainFSM
from db.models.organization import Organization
from services.cloudflare_api import (
    CloudflareAPIError,
    create_a_record,
    delete_a_record,
    find_zone,
    get_zone,
    list_dns_records,
    list_zones,
    update_a_record,
)
from services.cloudflare_service import CloudflareService

router = Router()

_PAGE = 7
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _valid_ip(ip: str) -> bool:
    if not _IP_RE.match(ip):
        return False
    return all(0 <= int(o) <= 255 for o in ip.split("."))


async def _get_cf(session: AsyncSession, org_id: int):
    cf = await CloudflareService(session).get_or_create(org_id)
    return cf if (cf.email and cf.api_key) else None


def _cf_not_configured_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings:cloudflare")],
        [InlineKeyboardButton(text="🔽 Назад", callback_data="menu:domains")],
    ])


def _back_to_records_kb(zone_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔽 К записям зоны", callback_data=f"dom:zone:{zone_id}:0")],
    ])


# ── Entry ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:domains")
async def domains_menu_cb(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text("🌐 Управление доменами", reply_markup=domains_menu_kb())


# ── Zone list ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dom:list:"))
async def zone_list_cb(call: CallbackQuery, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    page = int(call.data.split(":")[2])

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await call.message.edit_text(
            "❌ Cloudflare не настроен.",
            reply_markup=_cf_not_configured_kb(),
        )
        return

    await call.message.edit_text("🔍 Загружаю список доменов...")
    try:
        zones = await list_zones(cf.email, cf.api_key)
    except CloudflareAPIError as e:
        await call.message.edit_text(f"❌ Ошибка Cloudflare API:\n{e}", reply_markup=domains_menu_kb())
        return

    if not zones:
        await call.message.edit_text("Домены не найдены в Cloudflare аккаунте.", reply_markup=domains_menu_kb())
        return

    total_pages = max(1, math.ceil(len(zones) / _PAGE))
    page = max(0, min(page, total_pages - 1))
    page_zones = zones[page * _PAGE:(page + 1) * _PAGE]

    await call.message.edit_text(
        f"🌐 Домены Cloudflare ({len(zones)}):",
        reply_markup=domains_zones_kb(page_zones, page, total_pages),
    )


# ── Record list ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dom:zone:"))
async def record_list_cb(call: CallbackQuery, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    parts = call.data.split(":")
    zone_id = parts[2]
    page = int(parts[3])

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await call.message.edit_text("❌ Cloudflare не настроен.", reply_markup=_cf_not_configured_kb())
        return

    await call.message.edit_text("🔍 Загружаю записи...")
    try:
        records = await list_dns_records(cf.email, cf.api_key, zone_id)
    except CloudflareAPIError as e:
        await call.message.edit_text(f"❌ Ошибка Cloudflare API:\n{e}", reply_markup=domains_menu_kb())
        return

    total_pages = max(1, math.ceil(len(records) / _PAGE)) if records else 1
    page = max(0, min(page, total_pages - 1))
    offset = page * _PAGE
    page_records = records[offset:offset + _PAGE]

    try:
        zone = await get_zone(cf.email, cf.api_key, zone_id)
        zone_name = zone["name"] if zone else zone_id
    except CloudflareAPIError:
        zone_name = zone_id
    text = (
        f"📋 {zone_name} — {len(records)} записей"
        if records
        else f"📋 {zone_name}\n\nЗаписей нет."
    )
    await call.message.edit_text(
        text,
        reply_markup=domains_records_kb(page_records, zone_id, page, total_pages),
    )


# ── Record detail ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dom:rec:"))
async def record_detail_cb(call: CallbackQuery, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    parts = call.data.split(":")
    zone_id = parts[2]
    ridx = int(parts[3])

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await call.message.edit_text("❌ Cloudflare не настроен.", reply_markup=_cf_not_configured_kb())
        return

    try:
        records = await list_dns_records(cf.email, cf.api_key, zone_id)
    except CloudflareAPIError as e:
        await call.message.edit_text(f"❌ Ошибка:\n{e}", reply_markup=domains_menu_kb())
        return

    if ridx >= len(records):
        await call.answer("Запись не найдена. Обновите список.", show_alert=True)
        return

    record = records[ridx]

    if record["type"] != "A":
        await call.message.edit_text(
            f"ℹ️ {record['name']}\nТип: {record['type']}\n\nРедактирование поддерживается только для A-записей.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔽 Назад", callback_data=f"dom:zone:{zone_id}:0")],
            ]),
        )
        return

    ttl_str = "Auto" if record.get("ttl") == 1 else str(record.get("ttl", "—"))
    proxied = "✅ Да" if record.get("proxied") else "❌ Нет"
    text = (
        f"📍 A-запись\n\n"
        f"Имя: {record['name']}\n"
        f"IP: <code>{record['content']}</code>\n"
        f"TTL: {ttl_str}\n"
        f"Проксирование CF: {proxied}"
    )
    await call.message.edit_text(
        text,
        reply_markup=domains_record_detail_kb(zone_id, ridx),
        parse_mode="HTML",
    )


# ── Add A record ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dom:adda:"))
async def add_a_start(call: CallbackQuery, state: FSMContext, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    zone_id = call.data.split(":")[2]

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await call.message.edit_text("❌ Cloudflare не настроен.", reply_markup=_cf_not_configured_kb())
        return

    try:
        zone = await get_zone(cf.email, cf.api_key, zone_id)
        zone_name = zone["name"] if zone else zone_id
    except CloudflareAPIError:
        zone_name = zone_id

    await state.set_state(DomainFSM.waiting_new_a_name)
    await state.update_data(zone_id=zone_id, zone_name=zone_name)
    await call.message.edit_text(
        f"➕ Добавление A-записи\nЗона: <b>{zone_name}</b>\n\n"
        f"Введите имя хоста:\n"
        f"<i>Например: <code>node1</code> или <code>node1.{zone_name}</code></i>",
        reply_markup=domains_cancel_kb(zone_id),
        parse_mode="HTML",
    )


@router.message(DomainFSM.waiting_new_a_name)
async def add_a_got_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    data = await state.get_data()
    zone_id = data["zone_id"]
    zone_name = data["zone_name"]

    if not name:
        await message.answer("Имя не может быть пустым.", reply_markup=domains_cancel_kb(zone_id))
        return

    await state.update_data(record_name=name)
    await state.set_state(DomainFSM.waiting_new_a_ip)
    await message.answer(
        f"Имя: <code>{name}.{zone_name}</code>\n\nВведите IP-адрес:",
        reply_markup=domains_cancel_kb(zone_id),
        parse_mode="HTML",
    )


@router.message(DomainFSM.waiting_new_a_ip)
async def add_a_got_ip(message: Message, state: FSMContext, active_org: Organization, session: AsyncSession) -> None:
    ip = (message.text or "").strip()
    data = await state.get_data()
    zone_id = data["zone_id"]
    zone_name = data["zone_name"]
    record_name = data["record_name"]

    if not _valid_ip(ip):
        await message.answer("❌ Неверный формат IP. Введите IPv4 (например: 1.2.3.4):", reply_markup=domains_cancel_kb(zone_id))
        return

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await state.clear()
        await message.answer("❌ Cloudflare не настроен.")
        return

    try:
        await create_a_record(cf.email, cf.api_key, zone_id, record_name, ip)
    except CloudflareAPIError as e:
        await message.answer(f"❌ Ошибка Cloudflare API:\n{e}", reply_markup=domains_cancel_kb(zone_id))
        return

    await state.clear()
    await message.answer(
        f"✅ A-запись создана:\n<code>{record_name}.{zone_name}</code> → <code>{ip}</code>",
        reply_markup=_back_to_records_kb(zone_id),
        parse_mode="HTML",
    )


# ── Edit A record ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dom:edit:"))
async def edit_a_start(call: CallbackQuery, state: FSMContext, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    parts = call.data.split(":")
    zone_id = parts[2]
    ridx = int(parts[3])

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await call.message.edit_text("❌ Cloudflare не настроен.", reply_markup=_cf_not_configured_kb())
        return

    try:
        records = await list_dns_records(cf.email, cf.api_key, zone_id)
    except CloudflareAPIError as e:
        await call.message.edit_text(f"❌ Ошибка:\n{e}", reply_markup=domains_menu_kb())
        return

    if ridx >= len(records):
        await call.answer("Запись не найдена.", show_alert=True)
        return

    record = records[ridx]
    await state.set_state(DomainFSM.waiting_edit_ip)
    await state.update_data(zone_id=zone_id, record_id=record["id"], record_name=record["name"], ridx=ridx)
    await call.message.edit_text(
        f"✏️ Редактирование A-записи\n\n"
        f"Имя: {record['name']}\n"
        f"Текущий IP: <code>{record['content']}</code>\n\n"
        f"Введите новый IP:",
        reply_markup=domains_cancel_kb(zone_id),
        parse_mode="HTML",
    )


@router.message(DomainFSM.waiting_edit_ip)
async def edit_a_got_ip(message: Message, state: FSMContext, active_org: Organization, session: AsyncSession) -> None:
    ip = (message.text or "").strip()
    data = await state.get_data()
    zone_id = data["zone_id"]
    record_id = data["record_id"]
    record_name = data["record_name"]
    ridx = data["ridx"]

    if not _valid_ip(ip):
        await message.answer("❌ Неверный формат IP. Введите IPv4:", reply_markup=domains_cancel_kb(zone_id))
        return

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await state.clear()
        await message.answer("❌ Cloudflare не настроен.")
        return

    try:
        await update_a_record(cf.email, cf.api_key, zone_id, record_id, record_name, ip)
    except CloudflareAPIError as e:
        await message.answer(f"❌ Ошибка Cloudflare API:\n{e}", reply_markup=domains_cancel_kb(zone_id))
        return

    await state.clear()
    await message.answer(
        f"✅ A-запись обновлена:\n<code>{record_name}</code> → <code>{ip}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Открыть запись", callback_data=f"dom:rec:{zone_id}:{ridx}")],
            [InlineKeyboardButton(text="🔽 К записям зоны", callback_data=f"dom:zone:{zone_id}:0")],
        ]),
        parse_mode="HTML",
    )


# ── Delete A record ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dom:del:"))
async def delete_confirm_cb(call: CallbackQuery, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    parts = call.data.split(":")
    zone_id = parts[2]
    ridx = int(parts[3])

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await call.message.edit_text("❌ Cloudflare не настроен.", reply_markup=_cf_not_configured_kb())
        return

    try:
        records = await list_dns_records(cf.email, cf.api_key, zone_id)
    except CloudflareAPIError as e:
        await call.message.edit_text(f"❌ Ошибка:\n{e}", reply_markup=domains_menu_kb())
        return

    if ridx >= len(records):
        await call.answer("Запись не найдена.", show_alert=True)
        return

    record = records[ridx]
    await call.message.edit_text(
        f"🗑 Удалить A-запись?\n\n"
        f"Имя: {record['name']}\n"
        f"IP: {record['content']}",
        reply_markup=domains_delete_confirm_kb(zone_id, ridx),
    )


@router.callback_query(F.data.startswith("dom:delok:"))
async def delete_do_cb(call: CallbackQuery, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    parts = call.data.split(":")
    zone_id = parts[2]
    ridx = int(parts[3])

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await call.message.edit_text("❌ Cloudflare не настроен.", reply_markup=_cf_not_configured_kb())
        return

    try:
        records = await list_dns_records(cf.email, cf.api_key, zone_id)
    except CloudflareAPIError as e:
        await call.message.edit_text(f"❌ Ошибка:\n{e}", reply_markup=domains_menu_kb())
        return

    if ridx >= len(records):
        await call.answer("Запись не найдена.", show_alert=True)
        return

    record = records[ridx]
    try:
        await delete_a_record(cf.email, cf.api_key, zone_id, record["id"])
    except CloudflareAPIError as e:
        await call.message.edit_text(f"❌ Ошибка при удалении:\n{e}", reply_markup=domains_menu_kb())
        return

    await call.message.edit_text(
        f"✅ Запись удалена: {record['name']}",
        reply_markup=_back_to_records_kb(zone_id),
    )


# ── Manual search ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "dom:manual")
async def manual_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(DomainFSM.waiting_manual_domain)
    await call.message.edit_text(
        "🔍 Введите домен для поиска:\n\n"
        "<i>Принимается любой уровень: example.com или node.example.com</i>",
        reply_markup=domains_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(DomainFSM.waiting_manual_domain)
async def manual_got_domain(message: Message, state: FSMContext, active_org: Organization, session: AsyncSession) -> None:
    domain = (message.text or "").strip().lower().rstrip(".")

    if "." not in domain or len(domain) < 4:
        await message.answer(
            "❌ Введите корректный домен, например: <code>example.com</code>",
            reply_markup=domains_cancel_kb(),
            parse_mode="HTML",
        )
        return

    cf = await _get_cf(session, active_org.id)
    if not cf:
        await state.clear()
        await message.answer("❌ Cloudflare не настроен.", reply_markup=_cf_not_configured_kb())
        return

    searching = await message.answer(f"🔍 Ищу зону для {domain}...")
    try:
        zone_result = await find_zone(cf.email, cf.api_key, domain)
    except CloudflareAPIError as e:
        await state.clear()
        await searching.edit_text(f"❌ Ошибка Cloudflare API:\n{e}")
        return

    if not zone_result:
        await searching.edit_text(
            f"❌ Зона для домена <code>{domain}</code> не найдена в аккаунте Cloudflare.",
            parse_mode="HTML",
        )
        return

    zone_id, zone_name = zone_result
    await state.clear()

    try:
        records = await list_dns_records(cf.email, cf.api_key, zone_id)
    except CloudflareAPIError as e:
        await searching.edit_text(f"❌ Ошибка при загрузке записей:\n{e}")
        return

    total_pages = max(1, math.ceil(len(records) / _PAGE)) if records else 1
    page_records = records[0:_PAGE]
    text = (
        f"📋 {zone_name} — {len(records)} записей"
        if records
        else f"📋 {zone_name}\n\nЗаписей нет."
    )
    await searching.edit_text(
        text,
        reply_markup=domains_records_kb(page_records, zone_id, 0, total_pages),
    )


# ── Cancel FSM ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dom:cancel"))
async def cancel_cb(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    parts = call.data.split(":")
    # dom:cancel:{zone_id} → back to record list; dom:cancel → back to menu
    if len(parts) == 3:
        zone_id = parts[2]
        await call.message.edit_text(
            "❌ Отменено.",
            reply_markup=_back_to_records_kb(zone_id),
        )
    else:
        await call.message.edit_text("🌐 Управление доменами", reply_markup=domains_menu_kb())


# ── Noop ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "dom:noop")
async def noop_cb(call: CallbackQuery) -> None:
    await call.answer()
