"""Hosts FSM — update host IP addresses by tag.

Source modes:
  auto   — Pingachock finds reachable IPs from the set, picks best
  manual — user scrolls paginated list and picks manually

Distribution modes:
  same   — one IP applied to every host with the tag
  each   — each host gets a distinct IP (in order of latency for auto,
            in set order for manual)

Subnets in IP sets (e.g. 10.0.0.0/24) are always expanded to bare IPs
before use — hosts never receive CIDR notation.
"""
from __future__ import annotations
import asyncio
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    hosts_auto_confirm_kb,
    hosts_cancel_kb,
    hosts_confirm_bulk_kb,
    hosts_distribution_kb,
    hosts_ip_page_kb,
    hosts_ip_sets_kb,
    hosts_panels_kb,
    hosts_source_kb,
    hosts_tags_kb,
    hosts_top_mode_kb,
)
from bot.states.hosts import HostsFSM
from db.models.organization import Organization
from services.ip_check_service import CHECK_BATCH as _CHECK_BATCH, distributed_check as _distributed_check, expand_addresses as _expand_addresses
from services.ip_set_service import IpSetService
from services.pingachock_api_service import PingachockAPIError
from services.pingachock_service import PingachockService
from services.remnawave_api_service import (
    RemnaWaveAPIError,
    get_hosts,
    update_host_address,
)
from services.remnawave_service import RemnaWaveService

router = Router()

_PAGE = 8         # IPs per page in the manual paginated picker


async def _find_good_ips(
    api_url: str,
    api_key: str,
    all_ips: list[str],
    needed: int,
    progress_cb,  # async (from_n, to_n, total, found, needed) -> None
) -> tuple[list[str], int]:
    """Find `needed` reachable IPs, checking _CHECK_BATCH at a time.

    Calls progress_cb before each batch so the caller can update the UI.
    Returns (good_ips, total_checked).
    """
    good: list[str] = []
    offset = 0
    total = len(all_ips)

    while offset < total and len(good) < needed:
        batch = all_ips[offset: offset + _CHECK_BATCH]
        from_n = offset + 1
        to_n = offset + len(batch)
        offset += _CHECK_BATCH

        await progress_cb(from_n, to_n, total, len(good), needed)

        try:
            result = await _distributed_check(api_url, api_key, batch)
        except PingachockAPIError:
            continue  # skip bad batch, try next

        for ip in batch:
            if result.get(ip) and len(good) < needed:
                good.append(ip)

    return good, min(offset, total)


# ── panel / tag ───────────────────────────────────────────────────────────────

def _extract_tags(hosts: list[dict]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for host in hosts:
        for tag in host.get("tags") or []:
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
    return sorted(tags)


@router.callback_query(F.data == "menu:hosts")
async def hosts_menu(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_org_panels(active_org.id)
    if not panels:
        await call.message.edit_text(
            "❌ Нет настроенных панелей Remnawave.\n\nДобавьте панель в Настройках.",
            reply_markup=hosts_panels_kb([]),
        )
        return
    await state.set_state(HostsFSM.choosing_panel)
    await call.message.edit_text(
        "Выберите панель Remnawave:",
        reply_markup=hosts_panels_kb(panels),
    )


@router.callback_query(HostsFSM.choosing_panel, F.data.regexp(r"^hosts:panel:\d+$"))
async def panel_chosen(
    call: CallbackQuery,
    state: FSMContext,
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

    await call.message.edit_text("⏳ Загружаю хосты...")
    try:
        hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await call.message.edit_text(
            f"❌ Не удалось загрузить хосты:\n{e}",
            reply_markup=hosts_panels_kb(await svc.get_org_panels(active_org.id)),
        )
        return

    tags = _extract_tags(hosts)
    if not tags:
        await call.message.edit_text(
            "⚠️ У хостов этой панели нет тегов.",
            reply_markup=hosts_cancel_kb(),
        )
        return

    await state.update_data(panel_id=panel_id, tags=tags)
    await state.set_state(HostsFSM.choosing_tag)
    await call.message.edit_text(
        f"Найдено тегов: {len(tags)}\n\nВыберите тег:",
        reply_markup=hosts_tags_kb(tags),
    )


@router.callback_query(HostsFSM.choosing_tag, F.data.regexp(r"^hosts:tag:\d+$"))
async def tag_chosen(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    tags: list[str] = data["tags"]
    if idx >= len(tags):
        await call.answer("Тег не найден.", show_alert=True)
        return

    await state.update_data(selected_tag=tags[idx])
    await state.set_state(HostsFSM.choosing_top_mode)
    await call.message.edit_text(
        f"Тег: <b>{tags[idx]}</b>\n\nКак задать IP адреса хостам?",
        reply_markup=hosts_top_mode_kb(),
        parse_mode="HTML",
    )


# ── top mode ──────────────────────────────────────────────────────────────────

@router.callback_query(HostsFSM.choosing_top_mode, F.data == "hosts:top:manual")
async def top_manual(call: CallbackQuery, state: FSMContext) -> None:
    """Simple text input → apply same address to all hosts."""
    await call.answer()
    data = await state.get_data()
    tag: str = data["selected_tag"]
    await state.set_state(HostsFSM.waiting_address)
    await call.message.edit_text(
        f"Тег: <b>{tag}</b>\n\nВведите IP адрес или домен — он будет установлен всем хостам с этим тегом:",
        reply_markup=hosts_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(HostsFSM.waiting_address)
async def got_address(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    address = (message.text or "").strip()
    if not address:
        await message.answer("Введите IP адрес или домен:", reply_markup=hosts_cancel_kb())
        return
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    tag: str = data["selected_tag"]

    rw_svc = RemnaWaveService(session)
    panel = await rw_svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await state.clear()
        await message.answer("❌ Панель Remnawave не найдена.")
        return

    status_msg = await message.answer(
        f"⏳ Обновляю хосты с тегом <b>{tag}</b>...", parse_mode="HTML"
    )
    await state.clear()

    try:
        hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await status_msg.edit_text(f"❌ Не удалось загрузить хосты:\n{e}")
        return

    targets = [h for h in hosts if tag in (h.get("tags") or [])]
    if not targets:
        await status_msg.edit_text(
            f"⚠️ Хосты с тегом <b>{tag}</b> не найдены.", parse_mode="HTML"
        )
        return

    errors: list[str] = []

    async def _upd(host: dict) -> None:
        try:
            await update_host_address(panel.url, panel.api_token, host["uuid"], address)
        except RemnaWaveAPIError as e:
            errors.append(f"• {host.get('remark', host['uuid'])}: {str(e)[:60]}")

    await asyncio.gather(*[_upd(h) for h in targets])

    if errors:
        await status_msg.edit_text(
            f"⚠️ Обновлено {len(targets) - len(errors)}/{len(targets)} хостов.\n\n"
            "Ошибки:\n" + "\n".join(errors),
            parse_mode="HTML",
        )
    else:
        await status_msg.edit_text(
            f"✅ Готово!\n\n🏷 Тег: <b>{tag}</b>\n"
            f"🌐 Адрес: <code>{address}</code>\n"
            f"📡 Обновлено: {len(targets)} хостов",
            parse_mode="HTML",
        )


@router.callback_query(HostsFSM.choosing_top_mode, F.data == "hosts:top:ipsets")
async def top_ipsets(call: CallbackQuery, state: FSMContext) -> None:
    """Enter IP-sets sub-flow."""
    await call.answer()
    await state.set_state(HostsFSM.choosing_source)
    await call.message.edit_text(
        "Выберите режим подбора IP:",
        reply_markup=hosts_source_kb(),
    )


# ── source → distribution → ip set ───────────────────────────────────────────

async def _go_to_distribution(call: CallbackQuery, state: FSMContext, source: str) -> None:
    await state.update_data(source=source)
    await state.set_state(HostsFSM.choosing_distribution)
    await call.message.edit_text(
        "Как распределить IP по хостам?",
        reply_markup=hosts_distribution_kb(),
    )


@router.callback_query(HostsFSM.choosing_source, F.data == "hosts:source:auto")
async def source_auto(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    # Pre-check: Pingachock must be configured
    pc_svc = PingachockService(session)
    if not await pc_svc.get_settings(active_org.id):
        await call.message.edit_text(
            "❌ Pingachock не подключён.\n\n"
            "Перейдите в Настройки → Pingachock и добавьте API-ключ.",
            reply_markup=hosts_cancel_kb(),
        )
        return
    await _go_to_distribution(call, state, "auto")


@router.callback_query(HostsFSM.choosing_source, F.data == "hosts:source:manual")
async def source_manual(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await _go_to_distribution(call, state, "manual")


async def _pick_ip_set(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
    distribution: str,
) -> None:
    svc = IpSetService(session)
    sets = await svc.get_org_sets(active_org.id)
    if not sets:
        await call.message.edit_text(
            "❌ Нет сохранённых наборов IP.\n\nСначала добавьте набор в разделе «Наборы IP».",
            reply_markup=hosts_cancel_kb(),
        )
        return
    await state.update_data(distribution=distribution)
    await state.set_state(HostsFSM.choosing_ip_set)
    await call.message.edit_text(
        "Выберите набор IP:",
        reply_markup=hosts_ip_sets_kb(sets),
    )


@router.callback_query(HostsFSM.choosing_distribution, F.data == "hosts:dist:same")
async def dist_same(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    await _pick_ip_set(call, state, session, active_org, "same")


@router.callback_query(HostsFSM.choosing_distribution, F.data == "hosts:dist:each")
async def dist_each(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    await _pick_ip_set(call, state, session, active_org, "each")


# ── ip set chosen: branch by source + distribution ───────────────────────────

@router.callback_query(HostsFSM.choosing_ip_set, F.data.regexp(r"^hosts:ipset:\d+$"))
async def ipset_chosen(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    set_id = int(call.data.split(":")[2])
    ip_svc = IpSetService(session)
    ip_set = await ip_svc.get_set_by_id(set_id, active_org.id)
    if not ip_set:
        await call.answer("Набор не найден.", show_alert=True)
        return

    await state.update_data(set_id=set_id)
    data = await state.get_data()
    source: str = data["source"]
    distribution: str = data["distribution"]

    if source == "manual":
        await _handle_manual(call, state, session, active_org, ip_set, distribution)
    else:
        await _handle_auto(call, state, session, active_org, ip_set, distribution)


# ── MANUAL path ───────────────────────────────────────────────────────────────

async def _handle_manual(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
    ip_set,
    distribution: str,
) -> None:
    if distribution == "same":
        # Paginated picker — expand to bare IPs first
        ips, truncated = _expand_addresses(ip_set.addresses)
        if not ips:
            await call.message.edit_text(
                "❌ Набор не содержит валидных адресов.", reply_markup=hosts_cancel_kb()
            )
            return
        warn = f"\n⚠️ Показано первые {len(ips)} адресов (набор обрезан)." if truncated else ""
        await state.update_data(ip_page=0)
        await state.set_state(HostsFSM.choosing_single_ip)
        await _show_ip_page(call.message, ips, 0, ip_set.tag, extra_note=warn, edit=True)

    else:  # each
        await _build_manual_bulk_preview(call, state, session, active_org, ip_set)


async def _show_ip_page(
    message: Message,
    ips: list[str],
    page: int,
    set_tag: str,
    *,
    extra_note: str = "",
    edit: bool = True,
) -> None:
    start = page * _PAGE
    page_ips = ips[start: start + _PAGE]
    total_pages = max(1, (len(ips) + _PAGE - 1) // _PAGE)
    text = (
        f"Набор: <b>{set_tag}</b>\n"
        f"Адресов: {len(ips):,}"
        f"{extra_note}\n\n"
        f"Выберите IP (стр. {page + 1}/{total_pages}):"
    )
    kb = hosts_ip_page_kb(page_ips, page, page > 0, start + _PAGE < len(ips))
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(HostsFSM.choosing_single_ip, F.data.regexp(r"^hosts:ippage:\d+$"))
async def ip_page_nav(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    page = int(call.data.split(":")[2])
    data = await state.get_data()

    ip_svc = IpSetService(session)
    ip_set = await ip_svc.get_set_by_id(data["set_id"], active_org.id)
    if not ip_set:
        await call.answer("Набор не найден.", show_alert=True)
        return

    ips, truncated = _expand_addresses(ip_set.addresses)
    await state.update_data(ip_page=page)
    note = f"\n⚠️ Показаны первые {len(ips)} адресов." if truncated else ""
    await _show_ip_page(call.message, ips, page, ip_set.tag, extra_note=note, edit=True)


@router.callback_query(HostsFSM.choosing_single_ip, F.data.regexp(r"^hosts:ip:.+$"))
async def ip_selected(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    ip = call.data[len("hosts:ip:"):]
    await call.answer()
    await _apply_same_ip(call.message, state, session, active_org, ip)


async def _build_manual_bulk_preview(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
    ip_set,
) -> None:
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    tag: str = data["selected_tag"]

    rw_svc = RemnaWaveService(session)
    panel = await rw_svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await call.answer("Панель не найдена.", show_alert=True)
        return

    await call.message.edit_text("⏳ Загружаю хосты для превью...")
    try:
        hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await call.message.edit_text(f"❌ Ошибка Remnawave:\n{e}", reply_markup=hosts_cancel_kb())
        return

    targets = [h for h in hosts if tag in (h.get("tags") or [])]
    if not targets:
        await call.message.edit_text(
            f"⚠️ Хосты с тегом <b>{tag}</b> не найдены.", reply_markup=hosts_cancel_kb(),
            parse_mode="HTML",
        )
        return

    ips, _ = _expand_addresses(ip_set.addresses)
    apply_count = min(len(targets), len(ips))

    preview_lines: list[str] = []
    for h, ip in zip(targets[:20], ips[:20]):
        remark = (h.get("remark") or h["uuid"])[:30]
        preview_lines.append(f"<code>{remark}</code> → <code>{ip}</code>")
    preview = "\n".join(preview_lines)
    if apply_count > 20:
        preview += f"\n<i>...и ещё {apply_count - 20}</i>"

    warn = ""
    if len(ips) < len(targets):
        warn = (
            f"\n\n⚠️ В наборе {len(ips)} адресов, хостов {len(targets)} — "
            f"обновится {len(ips)}."
        )

    await state.set_state(HostsFSM.confirming_manual_bulk)
    await call.message.edit_text(
        f"🔀 <b>Каждому свой IP — вручную</b>\n\n"
        f"Тег: <b>{tag}</b>  |  Набор: <b>{ip_set.tag}</b>\n\n"
        f"{preview}{warn}\n\n"
        f"Будет обновлено хостов: <b>{apply_count}</b>",
        reply_markup=hosts_confirm_bulk_kb(),
        parse_mode="HTML",
    )


@router.callback_query(HostsFSM.confirming_manual_bulk, F.data == "hosts:bulk:confirm")
async def manual_bulk_confirm(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    tag: str = data["selected_tag"]
    set_id: int = data["set_id"]

    rw_svc = RemnaWaveService(session)
    panel = await rw_svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await state.clear()
        await call.message.edit_text("❌ Панель не найдена.")
        return

    ip_svc = IpSetService(session)
    ip_set = await ip_svc.get_set_by_id(set_id, active_org.id)
    if not ip_set:
        await state.clear()
        await call.message.edit_text("❌ Набор IP не найден.")
        return

    await call.message.edit_text(f"⏳ Обновляю хосты с тегом <b>{tag}</b>...", parse_mode="HTML")

    try:
        hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await state.clear()
        await call.message.edit_text(f"❌ Ошибка Remnawave:\n{e}")
        return

    targets = [h for h in hosts if tag in (h.get("tags") or [])]
    ips, _ = _expand_addresses(ip_set.addresses)
    pairs = list(zip(targets, ips))
    await state.clear()

    await _apply_pairs(call.message, pairs, panel, tag, ip_set.tag)


# ── AUTO path ─────────────────────────────────────────────────────────────────

async def _handle_auto(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
    ip_set,
    distribution: str,
) -> None:
    """Auto mode: find reachable IPs via TM Pingachock nodes, batch by batch."""
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    tag: str = data["selected_tag"]

    # Verify Pingachock configured
    pc_svc = PingachockService(session)
    pc_settings = await pc_svc.get_settings(active_org.id)
    if not pc_settings:
        await call.message.edit_text(
            "❌ Pingachock не подключён. Добавьте настройки в Настройки → Pingachock.",
            reply_markup=hosts_cancel_kb(),
        )
        return

    # Expand IP set to bare IPs
    all_ips, _ = _expand_addresses(ip_set.addresses)
    if not all_ips:
        await call.message.edit_text(
            "❌ Набор не содержит валидных адресов.", reply_markup=hosts_cancel_kb()
        )
        return

    # Load hosts to know how many IPs we need
    rw_svc = RemnaWaveService(session)
    panel = await rw_svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await call.message.edit_text("❌ Панель не найдена.", reply_markup=hosts_cancel_kb())
        return

    try:
        hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await call.message.edit_text(
            f"❌ Ошибка Remnawave:\n{e}", reply_markup=hosts_cancel_kb()
        )
        return

    targets = [h for h in hosts if tag in (h.get("tags") or [])]
    if not targets:
        await call.message.edit_text(
            f"⚠️ Хосты с тегом <b>{tag}</b> не найдены.",
            reply_markup=hosts_cancel_kb(),
            parse_mode="HTML",
        )
        return

    needed = 1 if distribution == "same" else len(targets)

    # ── progress tracking + elapsed-time ticker ───────────────────────────────
    start_t = time.monotonic()
    # base_text: static part of the progress message (NO elapsed suffix).
    # Both _progress and _ticker append fresh elapsed when calling edit_text.
    base_text: list[str] = [
        f"⏳ Запускаю проверку через ноды Туркменистана...\n\n"
        f"Набор: <b>{ip_set.tag}</b>  ({len(all_ips):,} адресов)\n"
        f"Нужно рабочих IP: {needed}"
    ]
    ticker_done: list[bool] = [False]

    def _elapsed_str() -> str:
        secs = int(time.monotonic() - start_t)
        m, s = divmod(secs, 60)
        return f"{m}м {s}с" if m else f"{s}с"

    status_msg = await call.message.edit_text(base_text[0], parse_mode="HTML")

    async def _ticker() -> None:
        """Update message with elapsed time every 15 seconds."""
        while not ticker_done[0]:
            await asyncio.sleep(15)
            if ticker_done[0]:
                break
            text = base_text[0] + f"\n\n⏱ Прошло {_elapsed_str()}"
            try:
                await asyncio.wait_for(
                    status_msg.edit_text(text, parse_mode="HTML"),
                    timeout=6,
                )
            except Exception:
                pass

    async def _progress(from_n: int, to_n: int, total: int, found: int, ned: int) -> None:
        # Store STATIC part (no elapsed) so the ticker can append fresh elapsed
        base_text[0] = (
            f"⏳ Проверяю через ноды Туркменистана...\n\n"
            f"Адреса {from_n}–{to_n} из {total}\n"
            f"Рабочих найдено: {found} из {ned}"
        )
        try:
            await status_msg.edit_text(
                base_text[0] + f"\n\n⏱ Прошло {_elapsed_str()}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    ticker_task = asyncio.create_task(_ticker())
    try:
        good_ips, checked = await _find_good_ips(
            pc_settings.api_url, pc_settings.api_key,
            all_ips, needed, _progress,
        )
    except PingachockAPIError as e:
        ticker_done[0] = True
        ticker_task.cancel()
        await status_msg.edit_text(
            f"❌ Ошибка Pingachock:\n<code>{e}</code>",
            reply_markup=hosts_cancel_kb(),
            parse_mode="HTML",
        )
        return
    finally:
        ticker_done[0] = True
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass

    if not good_ips:
        await status_msg.edit_text(
            f"⚠️ Среди проверенных адресов нет рабочих из ТМ.\n\n"
            f"Набор: <b>{ip_set.tag}</b>\n"
            f"Проверено: {checked} адресов",
            reply_markup=hosts_cancel_kb(),
            parse_mode="HTML",
        )
        return

    # ── "same" branch ─────────────────────────────────────────────────────────
    if distribution == "same":
        best_ip = good_ips[0]
        await state.update_data(auto_ip=best_ip)
        await state.set_state(HostsFSM.confirming_auto)
        await status_msg.edit_text(
            f"🤖 <b>Автовыбор — всем одинаковый IP</b>\n\n"
            f"Тег: <b>{tag}</b>  |  Набор: <b>{ip_set.tag}</b>\n"
            f"Проверено адресов: {checked}\n\n"
            f"✅ Рабочий IP: <code>{best_ip}</code>\n\n"
            f"Применить ко всем <b>{len(targets)}</b> хостам с тегом «{tag}»?",
            reply_markup=hosts_auto_confirm_kb(),
            parse_mode="HTML",
        )
        return

    # ── "each" branch ─────────────────────────────────────────────────────────
    pairs = list(zip(targets, good_ips))
    short = len(targets) - len(good_ips)

    preview_lines: list[str] = []
    for h, ip in pairs[:20]:
        remark = (h.get("remark") or h["uuid"])[:30]
        preview_lines.append(f"<code>{remark}</code> → <code>{ip}</code>")
    preview = "\n".join(preview_lines)
    if len(pairs) > 20:
        preview += f"\n<i>...и ещё {len(pairs) - 20}</i>"

    warn = ""
    if short > 0:
        warn = (
            f"\n\n⚠️ Рабочих IP меньше хостов — "
            f"{short} хост(ов) не получат обновления."
        )

    await state.update_data(auto_each_ips=[ip for _, ip in pairs])
    await state.set_state(HostsFSM.confirming_auto)
    await status_msg.edit_text(
        f"🤖 <b>Автовыбор — каждому свой IP</b>\n\n"
        f"Тег: <b>{tag}</b>  |  Набор: <b>{ip_set.tag}</b>\n"
        f"Проверено: {checked} адресов, рабочих: {len(good_ips)}\n\n"
        f"{preview}{warn}\n\n"
        f"Подтвердить обновление {len(pairs)} хостов?",
        reply_markup=hosts_auto_confirm_kb(),
        parse_mode="HTML",
    )


@router.callback_query(HostsFSM.confirming_auto, F.data == "hosts:auto:confirm")
async def auto_confirm(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    tag: str = data["selected_tag"]
    distribution: str = data["distribution"]

    rw_svc = RemnaWaveService(session)
    panel = await rw_svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await state.clear()
        await call.message.edit_text("❌ Панель не найдена.")
        return

    await call.message.edit_text(f"⏳ Обновляю хосты с тегом <b>{tag}</b>...", parse_mode="HTML")

    try:
        hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await state.clear()
        await call.message.edit_text(f"❌ Ошибка Remnawave:\n{e}")
        return

    targets = [h for h in hosts if tag in (h.get("tags") or [])]
    await state.clear()

    if distribution == "same":
        ip: str = data["auto_ip"]
        await _apply_same_ip(call.message, None, session, active_org, ip, prefetched=(panel, targets, tag))
    else:
        each_ips: list[str] = data.get("auto_each_ips", [])
        real_pairs = list(zip(targets, each_ips))
        await _apply_pairs(call.message, real_pairs, panel, tag, "Pingachock")


# ── apply helpers ─────────────────────────────────────────────────────────────

async def _apply_same_ip(
    message: Message,
    state: FSMContext | None,
    session: AsyncSession,
    active_org: Organization,
    ip: str,
    prefetched: tuple | None = None,
) -> None:
    """Apply one IP to all hosts with the tag. prefetched = (panel, targets, tag)."""
    if prefetched:
        panel, targets, tag = prefetched
        if state:
            await state.clear()
    else:
        data = await state.get_data()  # type: ignore[union-attr]
        panel_id: int = data["panel_id"]
        tag: str = data["selected_tag"]

        rw_svc = RemnaWaveService(session)
        panel = await rw_svc.get_panel_by_id(panel_id, active_org.id)
        if not panel:
            if state:
                await state.clear()
            await message.answer("❌ Панель Remnawave не найдена.")
            return

        status_msg = await message.answer(
            f"⏳ Обновляю хосты с тегом <b>{tag}</b>...", parse_mode="HTML"
        )
        if state:
            await state.clear()

        try:
            hosts = await get_hosts(panel.url, panel.api_token)
        except RemnaWaveAPIError as e:
            await status_msg.edit_text(f"❌ Не удалось загрузить хосты:\n{e}")
            return

        targets = [h for h in hosts if tag in (h.get("tags") or [])]
        if not targets:
            await status_msg.edit_text(
                f"⚠️ Хосты с тегом <b>{tag}</b> не найдены.", parse_mode="HTML"
            )
            return

        errors: list[str] = []

        async def _upd(host: dict) -> None:
            try:
                await update_host_address(panel.url, panel.api_token, host["uuid"], ip)
            except RemnaWaveAPIError as e:
                errors.append(f"• {host.get('remark', host['uuid'])}: {str(e)[:60]}")

        await asyncio.gather(*[_upd(h) for h in targets])

        if errors:
            await status_msg.edit_text(
                f"⚠️ Обновлено {len(targets) - len(errors)}/{len(targets)} хостов.\n\n"
                "Ошибки:\n" + "\n".join(errors),
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text(
                f"✅ Готово!\n\n🏷 Тег: <b>{tag}</b>\n"
                f"🌐 Адрес: <code>{ip}</code>\n"
                f"📡 Обновлено: {len(targets)} хостов",
                parse_mode="HTML",
            )
        return

    # --- prefetched path (from auto_confirm) ---
    errors: list[str] = []

    async def _upd_pre(host: dict) -> None:
        try:
            await update_host_address(panel.url, panel.api_token, host["uuid"], ip)
        except RemnaWaveAPIError as e:
            errors.append(f"• {host.get('remark', host['uuid'])}: {str(e)[:60]}")

    await asyncio.gather(*[_upd_pre(h) for h in targets])

    if errors:
        await message.edit_text(
            f"⚠️ Обновлено {len(targets) - len(errors)}/{len(targets)} хостов.\n\n"
            "Ошибки:\n" + "\n".join(errors),
            parse_mode="HTML",
        )
    else:
        await message.edit_text(
            f"✅ Готово!\n\n🏷 Тег: <b>{tag}</b>\n"
            f"🌐 Адрес: <code>{ip}</code>\n"
            f"📡 Обновлено: {len(targets)} хостов",
            parse_mode="HTML",
        )


async def _apply_pairs(
    message: Message,
    pairs: list[tuple[dict, str]],
    panel,
    tag: str,
    set_name: str,
) -> None:
    """Apply (host, ip) pairs in parallel."""
    if not pairs:
        await message.edit_text("⚠️ Нет пар для обновления.")
        return

    errors: list[str] = []

    async def _upd(host: dict, ip: str) -> None:
        try:
            await update_host_address(panel.url, panel.api_token, host["uuid"], ip)
        except RemnaWaveAPIError as e:
            errors.append(f"• {host.get('remark', host['uuid'])}: {str(e)[:60]}")

    await asyncio.gather(*[_upd(h, ip) for h, ip in pairs])

    if errors:
        await message.edit_text(
            f"⚠️ Обновлено {len(pairs) - len(errors)}/{len(pairs)} хостов.\n\n"
            "Ошибки:\n" + "\n".join(errors),
            parse_mode="HTML",
        )
    else:
        await message.edit_text(
            f"✅ Готово!\n\n🏷 Тег: <b>{tag}</b>\n"
            f"📋 Набор: <b>{set_name}</b>\n"
            f"📡 Обновлено: {len(pairs)} хостов",
            parse_mode="HTML",
        )


# ── back navigation ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "hosts:back")
async def hosts_back(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    """State-aware back button: each step returns to the previous one."""
    await call.answer()
    current = await state.get_state()
    data = await state.get_data()

    if current is None or current == HostsFSM.choosing_panel:
        # Nothing to go back to — drop to main menu
        await state.clear()
        from bot.keyboards.inline import main_menu_kb
        await call.message.edit_text("Вы в главном меню", reply_markup=main_menu_kb())

    elif current == HostsFSM.choosing_tag:
        # Back to panel list
        svc = RemnaWaveService(session)
        panels = await svc.get_org_panels(active_org.id)
        await state.set_state(HostsFSM.choosing_panel)
        await call.message.edit_text(
            "Выберите панель Remnawave:",
            reply_markup=hosts_panels_kb(panels),
        )

    elif current == HostsFSM.choosing_top_mode:
        # Back to tag list
        tags: list[str] = data.get("tags", [])
        await state.set_state(HostsFSM.choosing_tag)
        await call.message.edit_text(
            f"Найдено тегов: {len(tags)}\n\nВыберите тег:",
            reply_markup=hosts_tags_kb(tags),
        )

    elif current in (HostsFSM.waiting_address, HostsFSM.choosing_source):
        # Back to top-mode choice
        tag: str = data.get("selected_tag", "")
        await state.set_state(HostsFSM.choosing_top_mode)
        await call.message.edit_text(
            f"Тег: <b>{tag}</b>\n\nКак задать IP адреса хостам?",
            reply_markup=hosts_top_mode_kb(),
            parse_mode="HTML",
        )

    elif current == HostsFSM.choosing_distribution:
        # Back to source (auto / manual)
        await state.set_state(HostsFSM.choosing_source)
        await call.message.edit_text(
            "Выберите режим подбора IP:",
            reply_markup=hosts_source_kb(),
        )

    elif current == HostsFSM.choosing_ip_set:
        # Back to distribution choice
        await state.set_state(HostsFSM.choosing_distribution)
        await call.message.edit_text(
            "Как распределить IP по хостам?",
            reply_markup=hosts_distribution_kb(),
        )

    else:
        # choosing_single_ip / confirming_manual_bulk / confirming_auto → back to IP set list
        svc = IpSetService(session)
        sets = await svc.get_org_sets(active_org.id)
        await state.set_state(HostsFSM.choosing_ip_set)
        if sets:
            await call.message.edit_text(
                "Выберите набор IP:",
                reply_markup=hosts_ip_sets_kb(sets),
            )
        else:
            await call.message.edit_text(
                "❌ Наборов IP нет. Добавьте набор в разделе «Наборы IP».",
                reply_markup=hosts_cancel_kb(),
            )
