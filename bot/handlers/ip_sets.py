from __future__ import annotations
import io

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import ip_set_cancel_kb, ip_set_detail_kb, ip_sets_menu_kb, ip_sets_section_kb
from bot.states.ip_sets import AddIpSet
from db.models.ip_set import IpSet
from db.models.organization import Organization
from db.models.user import User
from services.audit_service import send_audit
from services.ip_check_service import normalize_addresses
from services.ip_set_service import IpSetService

router = Router()

_MAX_ENTRIES = 5_000_000


# ── helpers ──────────────────────────────────────────────────────────────────


def _sets_text(sets: list[IpSet]) -> str:
    if not sets:
        return "📋 Наборы IP\n\nУ вас нет сохранённых наборов."
    lines = "\n".join(
        f"• {s.tag} — {len(s.addresses.splitlines())} записей"
        for s in sets
    )
    return f"📋 Наборы IP\n\n{lines}"


async def _show_menu(
    target: CallbackQuery | Message,
    session: AsyncSession,
    active_org: Organization,
    edit: bool = True,
) -> None:
    svc = IpSetService(session)
    sets = await svc.get_org_sets(active_org.id)
    text = _sets_text(sets)
    kb = ip_sets_menu_kb(sets)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if edit:
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


# ── menu ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:ip_sets")
async def ip_sets_section(
    call: CallbackQuery,
) -> None:
    """Entry screen: split between user lists and managed pools."""
    await call.answer()
    await call.message.edit_text(
        "📋 <b>Наборы IP</b>\n\n"
        "Пользовательские списки — сырые наборы адресов, загружаемые вручную.\n"
        "Модерируемые пулы — автоматически проверяемые и оцениваемые адреса.",
        reply_markup=ip_sets_section_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "ipset:user_lists")
async def ip_sets_menu(
    call: CallbackQuery, active_org: Organization, session: AsyncSession,
) -> None:
    await call.answer()
    await _show_menu(call, session, active_org)


@router.callback_query(F.data == "ipset:back")
async def ipset_back(
    call: CallbackQuery, active_org: Organization, session: AsyncSession,
) -> None:
    await call.answer()
    await _show_menu(call, session, active_org)


@router.callback_query(F.data == "ipset:cancel")
async def ipset_cancel(
    call: CallbackQuery, state: FSMContext, active_org: Organization, session: AsyncSession,
) -> None:
    await call.answer()
    current = await state.get_state()

    if current == AddIpSet.waiting_tag:
        # Back to the address-entry step
        await state.set_state(AddIpSet.waiting_addresses)
        await call.message.edit_text(
            "➕ <b>Новый набор IP</b>\n\n"
            "Отправьте список адресов в любом формате — IP автоматически извлекаются из текста.\n"
            "Поддерживаются одиночные IP и подсети CIDR любого масштаба:\n"
            "<code>1.2.3.4\n10.0.0.0/8\n2001:db8::/32</code>\n\n"
            "Или прикрепите <b>.txt файл</b> с адресами.",
            reply_markup=ip_set_cancel_kb(),
            parse_mode="HTML",
        )
    else:
        # waiting_addresses or no state → back to menu
        await state.clear()
        await _show_menu(call, session, active_org)


# ── view ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^ipset:view:(\d+)$"))
async def ipset_view(
    call: CallbackQuery, active_org: Organization, session: AsyncSession,
) -> None:
    await call.answer()
    set_id = int(call.data.split(":")[2])
    svc = IpSetService(session)
    ip_set = await svc.get_set_by_id(set_id, active_org.id)
    if not ip_set:
        await call.answer("Набор не найден.", show_alert=True)
        return

    count = len(ip_set.addresses.splitlines())
    content = ip_set.addresses.encode()
    file = BufferedInputFile(content, filename=f"{ip_set.tag}.txt")

    # Send the file
    await call.message.answer_document(file, caption=f"📋 {ip_set.tag} — {count} записей")

    # Update the control message to show detail keyboard
    await call.message.edit_text(
        f"📋 <b>{ip_set.tag}</b>\n{count} записей\n\nЧто сделать с этим набором?",
        reply_markup=ip_set_detail_kb(set_id),
        parse_mode="HTML",
    )


# ── delete ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^ipset:delete:(\d+)$"))
async def ipset_delete(
    call: CallbackQuery, active_org: Organization, session: AsyncSession, db_user: User,
) -> None:
    await call.answer()
    set_id = int(call.data.split(":")[2])
    svc = IpSetService(session)
    ip_set = await svc.get_set_by_id(set_id, active_org.id)
    if not ip_set:
        await call.answer("Набор не найден.", show_alert=True)
        return
    tag = ip_set.tag
    count = len(ip_set.addresses.splitlines())
    await svc.delete_set(set_id, active_org.id)
    send_audit(call.bot, active_org.id, db_user, f"Удалил набор IP: {tag} ({count:,} записей)")
    await _show_menu(call, session, active_org)
    await call.answer(f"✅ Набор «{tag}» удалён.", show_alert=False)


# ── add: step 1 — collect addresses ──────────────────────────────────────────

@router.callback_query(F.data == "ipset:add")
async def ipset_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AddIpSet.waiting_addresses)
    await call.message.edit_text(
        "➕ <b>Новый набор IP</b>\n\n"
        "Отправьте список адресов в любом формате — IP автоматически извлекаются из текста.\n"
        "Поддерживаются одиночные IP и подсети CIDR любого масштаба:\n"
        "<code>1.2.3.4\n10.0.0.0/8\n2001:db8::/32</code>\n\n"
        "Или прикрепите <b>.txt файл</b> с адресами.",
        reply_markup=ip_set_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AddIpSet.waiting_addresses, F.document)
async def ipset_got_file(
    message: Message, state: FSMContext,
) -> None:
    doc = message.document
    file = await message.bot.get_file(doc.file_id)
    buf = await message.bot.download_file(file.file_path)
    try:
        raw = buf.read().decode("utf-8", errors="replace")
    except Exception:
        await message.answer("❌ Не удалось прочитать файл.", reply_markup=ip_set_cancel_kb())
        return

    await _process_addresses(message, state, raw)


@router.message(AddIpSet.waiting_addresses)
async def ipset_got_text(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer(
            "Список не может быть пустым. Отправьте адреса или файл:",
            reply_markup=ip_set_cancel_kb(),
        )
        return
    await _process_addresses(message, state, raw)


async def _process_addresses(message: Message, state: FSMContext, raw: str) -> None:
    valid, skipped = normalize_addresses(raw, cap=_MAX_ENTRIES + 1)

    if not valid:
        await message.answer(
            "❌ Не найдено ни одного IP-адреса в тексте.\n\n"
            "Отправьте список адресов или файл с адресами:",
            reply_markup=ip_set_cancel_kb(),
        )
        return

    if len(valid) > _MAX_ENTRIES:
        await message.answer(
            f"❌ Слишком много записей ({len(valid):,}). Максимум {_MAX_ENTRIES:,}.",
            reply_markup=ip_set_cancel_kb(),
        )
        return

    addresses_text = "\n".join(valid)
    await state.update_data(addresses=addresses_text)
    await state.set_state(AddIpSet.waiting_tag)

    warn = f"\n⚠️ Пропущено нераспознанных фрагментов: {skipped}" if skipped else ""

    await message.answer(
        f"✅ Принято {len(valid):,} адресов.{warn}\n\n"
        "Введите тег для этого набора (например: <code>RU-subnets</code>):",
        reply_markup=ip_set_cancel_kb(),
        parse_mode="HTML",
    )


# ── add: step 2 — tag ────────────────────────────────────────────────────────

@router.message(AddIpSet.waiting_tag)
async def ipset_got_tag(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
    db_user: User,
) -> None:
    tag = (message.text or "").strip()
    if not tag:
        await message.answer("Тег не может быть пустым:", reply_markup=ip_set_cancel_kb())
        return
    if len(tag) > 255:
        await message.answer("Тег слишком длинный (макс. 255 символов):", reply_markup=ip_set_cancel_kb())
        return

    data = await state.get_data()
    addresses: str = data["addresses"]
    count = len(addresses.splitlines())

    svc = IpSetService(session)
    await svc.add_set(active_org.id, tag, addresses)
    await state.clear()

    send_audit(message.bot, active_org.id, db_user, f"Добавил набор IP: {tag} ({count:,} записей)")
    # Show updated menu
    await _show_menu(message, session, active_org, edit=False)
    await message.answer(f"✅ Набор <b>{tag}</b> ({count:,} записей) сохранён.", parse_mode="HTML")
