"""Handlers for the Automations section: Поддержание доступности IP.

UI flow (non-FSM):
  menu:automations → avail:list → avail:group:{id} → detail actions

FSM flow (adding a new group):
  avail:add → AvailGroupFSM.choosing_panel
            → AvailGroupFSM.choosing_tag
            → AvailGroupFSM.choosing_source_type   (user sets vs managed pool)
            ├── [sets]  → AvailGroupFSM.choosing_ip_sets  (multi-select)
            └── [pool]  → AvailGroupFSM.choosing_pool
            → AvailGroupFSM.choosing_distribution
            → AvailGroupFSM.choosing_interval
            → AvailGroupFSM.confirming
            → avail:confirm_create → save + return to list

avail:back  — state-aware; goes one step back.
avail:skip:{id} — cancel ongoing replacement search for a group.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    avail_confirm_kb,
    avail_delete_confirm_kb,
    avail_distribution_kb,
    avail_group_detail_kb,
    avail_groups_kb,
    avail_interval_kb,
    avail_ip_sets_kb,
    avail_panels_kb,
    avail_pools_kb,
    avail_source_type_kb,
    avail_tags_kb,
    main_menu_kb,
)
from bot.states.automation import AvailGroupFSM
from db.models.organization import Organization
from db.models.user import User
from services.audit_service import send_audit
from services.automation_service import AutomationService
from services.availability_monitor import _processing_groups, request_skip, run_group_check_now
from services.ip_set_service import IpSetService
from services.managed_pool_service import ManagedPoolService
from services.remnawave_api_service import RemnaWaveAPIError, get_hosts
from services.remnawave_service import RemnaWaveService

router = Router()


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_tags(hosts: list[dict]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for host in hosts:
        for tag in host.get("tags") or []:
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
    return sorted(tags)


async def _show_groups_list(
    call: CallbackQuery,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    auto_svc = AutomationService(session)
    rw_svc = RemnaWaveService(session)
    groups = await auto_svc.get_org_groups(active_org.id)
    panels = await rw_svc.get_org_panels(active_org.id)
    panels_by_id = {p.id: p for p in panels}

    active_count = sum(1 for g in groups if g.enabled)
    if groups:
        header = (
            f"🤖 <b>Автоматизации</b>\n\n"
            f"🔄 Поддержание доступности IP\n"
            f"Групп: {len(groups)}, активных: {active_count}"
        )
    else:
        header = (
            "🤖 <b>Автоматизации</b>\n\n"
            "🔄 Поддержание доступности IP\n"
            "Групп пока нет. Добавьте первую!"
        )
    await call.message.edit_text(
        header,
        reply_markup=avail_groups_kb(groups, panels_by_id),
        parse_mode="HTML",
    )


# ── entry points (non-FSM) ────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:automations")
async def automations_menu(
    call: CallbackQuery,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    await _show_groups_list(call, session, active_org)


@router.callback_query(F.data == "avail:list")
async def avail_list(
    call: CallbackQuery,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    await _show_groups_list(call, session, active_org)


# ── group detail ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^avail:group:\d+$"))
async def avail_group_detail(
    call: CallbackQuery,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    group_id = int(call.data.split(":")[2])
    auto_svc = AutomationService(session)
    group = await auto_svc.get_group(group_id, active_org.id)
    if not group:
        await call.answer("Группа не найдена.", show_alert=True)
        return

    rw_svc = RemnaWaveService(session)
    panel = await rw_svc.get_panel_by_id(group.panel_id, active_org.id)
    panel_label = panel.tag if panel else "?"

    if group.managed_pool_id:
        pool_svc = ManagedPoolService(session)
        pool = await pool_svc.get_pool_any(group.managed_pool_id)
        source_label = f"Управляемый пул: {pool.name}" if pool else f"Пул ID {group.managed_pool_id}"
    else:
        ip_svc = IpSetService(session)
        sets = await ip_svc.get_sets_by_ids(list(group.ip_set_ids))
        sets_label = ", ".join(s.tag for s in sets) if sets else "—"
        source_label = f"Наборы IP: {sets_label}"

    dist_label = "Всем одинаковый IP" if group.distribution == "same" else "Каждому свой IP"
    status_label = "✅ Активна" if group.enabled else "❌ Приостановлена"

    if group.last_checked_at:
        from datetime import timezone
        last = group.last_checked_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        last_str = last.strftime("%Y-%m-%d %H:%M UTC")
    else:
        last_str = "ещё не проверялась"

    await call.message.edit_text(
        f"📡 <b>Группа: {group.host_tag}</b>\n\n"
        f"Панель: <b>{panel_label}</b>\n"
        f"{source_label}\n"
        f"Режим: {dist_label}\n"
        f"Интервал: каждые {group.interval_minutes} мин\n"
        f"Статус: {status_label}\n"
        f"Последняя проверка: {last_str}",
        reply_markup=avail_group_detail_kb(group.id, group.enabled),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^avail:toggle:\d+$"))
async def avail_group_toggle(
    call: CallbackQuery,
    session: AsyncSession,
    active_org: Organization,
    db_user: User,
) -> None:
    await call.answer()
    group_id = int(call.data.split(":")[2])
    auto_svc = AutomationService(session)
    group = await auto_svc.toggle_enabled(group_id, active_org.id)
    if not group:
        await call.answer("Группа не найдена.", show_alert=True)
        return
    status = "▶️ возобновлена" if group.enabled else "⏸ приостановлена"
    await call.answer(f"Группа {status}.", show_alert=False)
    send_audit(
        call.bot, active_org.id, db_user,
        f"{'Возобновил' if group.enabled else 'Приостановил'} группу автоматизации: {group.host_tag}",
    )
    # Refresh detail view
    await avail_group_detail(call, session, active_org)


@router.callback_query(F.data.regexp(r"^avail:delete_confirm:\d+$"))
async def avail_delete_confirm(
    call: CallbackQuery,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    group_id = int(call.data.split(":")[2])
    auto_svc = AutomationService(session)
    group = await auto_svc.get_group(group_id, active_org.id)
    if not group:
        await call.answer("Группа не найдена.", show_alert=True)
        return
    await call.message.edit_text(
        f"🗑 Удалить группу <b>{group.host_tag}</b>?\n\n"
        "Это действие необратимо.",
        reply_markup=avail_delete_confirm_kb(group_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^avail:delete:\d+$"))
async def avail_group_delete(
    call: CallbackQuery,
    session: AsyncSession,
    active_org: Organization,
    db_user: User,
) -> None:
    await call.answer()
    group_id = int(call.data.split(":")[2])
    auto_svc = AutomationService(session)
    group = await auto_svc.get_group(group_id, active_org.id)
    if not group:
        await call.answer("Группа не найдена.", show_alert=True)
        return
    host_tag = group.host_tag
    deleted = await auto_svc.delete_group(group_id, active_org.id)
    if not deleted:
        await call.answer("Группа не найдена.", show_alert=True)
        return
    await call.answer("✅ Группа удалена.", show_alert=False)
    send_audit(call.bot, active_org.id, db_user, f"Удалил группу автоматизации: {host_tag}")
    await _show_groups_list(call, session, active_org)


# ── cancel/skip button from alert notifications ───────────────────────────────

@router.callback_query(F.data.regexp(r"^avail:skip:\d+$"))
async def avail_skip_check(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":")[2])
    if group_id in _processing_groups:
        request_skip(group_id)
        await call.answer("⏭ Поиск замены будет остановлен", show_alert=False)
    else:
        await call.answer("⏰ Время на отмену истекло", show_alert=True)


# ── FSM: start adding a group ─────────────────────────────────────────────────

@router.callback_query(F.data == "avail:add")
async def avail_add_start(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    rw_svc = RemnaWaveService(session)
    panels = await rw_svc.get_org_panels(active_org.id)
    if not panels:
        await call.message.edit_text(
            "❌ Нет настроенных панелей Remnawave.\n\n"
            "Добавьте панель в Настройках → Remnawave.",
            reply_markup=avail_panels_kb([]),
        )
        return
    await state.set_state(AvailGroupFSM.choosing_panel)
    await call.message.edit_text(
        "Выберите панель Remnawave:",
        reply_markup=avail_panels_kb(panels),
    )


# ── FSM step 1: panel chosen ──────────────────────────────────────────────────

@router.callback_query(AvailGroupFSM.choosing_panel, F.data.regexp(r"^avail:panel:\d+$"))
async def avail_panel_chosen(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])
    rw_svc = RemnaWaveService(session)
    panel = await rw_svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await call.answer("Панель не найдена.", show_alert=True)
        return

    await call.message.edit_text("⏳ Загружаю теги хостов...")
    try:
        hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await call.message.edit_text(
            f"❌ Не удалось загрузить хосты:\n{e}",
            reply_markup=avail_panels_kb(await rw_svc.get_org_panels(active_org.id)),
        )
        return

    tags = _extract_tags(hosts)
    if not tags:
        await call.message.edit_text(
            "⚠️ У хостов этой панели нет тегов.\n\nВыберите другую панель:",
            reply_markup=avail_panels_kb(await rw_svc.get_org_panels(active_org.id)),
        )
        return

    await state.update_data(panel_id=panel_id, panel_tag=panel.tag, tags=tags)
    await state.set_state(AvailGroupFSM.choosing_tag)
    await call.message.edit_text(
        f"Панель: <b>{panel.tag}</b>\n\nВыберите тег хостов для мониторинга:",
        reply_markup=avail_tags_kb(tags),
        parse_mode="HTML",
    )


# ── FSM step 2: tag chosen ────────────────────────────────────────────────────

@router.callback_query(AvailGroupFSM.choosing_tag, F.data.regexp(r"^avail:tag:\d+$"))
async def avail_tag_chosen(
    call: CallbackQuery,
    state: FSMContext,
) -> None:
    await call.answer()
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    tags: list[str] = data["tags"]
    if idx >= len(tags):
        await call.answer("Тег не найден.", show_alert=True)
        return
    tag = tags[idx]
    await state.update_data(selected_tag=tag, selected_set_ids=[], managed_pool_id=None)
    await state.set_state(AvailGroupFSM.choosing_source_type)
    await call.message.edit_text(
        f"Панель: <b>{data['panel_tag']}</b>  |  Тег: <b>{tag}</b>\n\n"
        "Источник IP-адресов для замен:",
        reply_markup=avail_source_type_kb(),
        parse_mode="HTML",
    )


# ── FSM step 3: source type ───────────────────────────────────────────────────

@router.callback_query(AvailGroupFSM.choosing_source_type, F.data == "avail:source:sets")
async def avail_source_sets(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    data = await state.get_data()

    ip_svc = IpSetService(session)
    sets = await ip_svc.get_org_sets(active_org.id)
    if not sets:
        await call.message.edit_text(
            "❌ Нет сохранённых наборов IP.\n\nСначала добавьте набор в «Наборы IP → Пользовательские списки».",
            reply_markup=avail_source_type_kb(),
        )
        return

    sets_info = [
        {"id": s.id, "tag": s.tag, "count": len(s.addresses.splitlines())}
        for s in sets
    ]
    await state.update_data(sets_info=sets_info, selected_set_ids=[], managed_pool_id=None)
    await state.set_state(AvailGroupFSM.choosing_ip_sets)
    await call.message.edit_text(
        f"Панель: <b>{data['panel_tag']}</b>  |  Тег: <b>{data['selected_tag']}</b>\n\n"
        "Выберите один или несколько наборов IP\n"
        "(IP из всех выбранных наборов объединяются в один пул):",
        reply_markup=avail_ip_sets_kb(sets_info, []),
        parse_mode="HTML",
    )


@router.callback_query(AvailGroupFSM.choosing_source_type, F.data == "avail:source:pool")
async def avail_source_pool(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    pool_svc = ManagedPoolService(session)
    pools = await pool_svc.get_org_pools(active_org.id)
    if not pools:
        await call.message.edit_text(
            "❌ Нет управляемых пулов.\n\nСоздайте пул в «Наборы IP → Модерируемые пулы».",
            reply_markup=avail_source_type_kb(),
        )
        return
    await state.update_data(sets_info=[], selected_set_ids=[], managed_pool_id=None)
    await state.set_state(AvailGroupFSM.choosing_pool)
    await call.message.edit_text(
        "Выберите управляемый пул:",
        reply_markup=avail_pools_kb(pools),
    )


@router.callback_query(AvailGroupFSM.choosing_pool, F.data.regexp(r"^avail:pool:\d+$"))
async def avail_pool_chosen(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    pool_id = int(call.data.split(":")[2])
    await state.update_data(managed_pool_id=pool_id, selected_set_ids=[])
    await state.set_state(AvailGroupFSM.choosing_distribution)
    await call.message.edit_text(
        "Как заменять IP хостам?",
        reply_markup=avail_distribution_kb(),
    )


# ── FSM step 4: toggle IP set selection ──────────────────────────────────────

@router.callback_query(AvailGroupFSM.choosing_ip_sets, F.data.regexp(r"^avail:toggle_set:\d+$"))
async def avail_toggle_set(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    set_id = int(call.data.split(":")[2])
    data = await state.get_data()
    selected: list[int] = list(data.get("selected_set_ids", []))
    if set_id in selected:
        selected.remove(set_id)
    else:
        selected.append(set_id)
    await state.update_data(selected_set_ids=selected)

    sets_info: list[dict] = data["sets_info"]
    await call.message.edit_reply_markup(
        reply_markup=avail_ip_sets_kb(sets_info, selected)
    )


@router.callback_query(AvailGroupFSM.choosing_ip_sets, F.data == "avail:confirm_sets")
async def avail_confirm_sets(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    selected: list[int] = data.get("selected_set_ids", [])
    if not selected:
        await call.answer("Выберите хотя бы один набор.", show_alert=True)
        return

    await state.set_state(AvailGroupFSM.choosing_distribution)
    await call.message.edit_text(
        "Как заменять IP хостам?",
        reply_markup=avail_distribution_kb(),
    )


# ── FSM step 4: distribution ──────────────────────────────────────────────────

@router.callback_query(AvailGroupFSM.choosing_distribution, F.data.regexp(r"^avail:dist:(same|each)$"))
async def avail_distribution_chosen(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    distribution = call.data.split(":")[2]
    await state.update_data(distribution=distribution)
    await state.set_state(AvailGroupFSM.choosing_interval)

    data = await state.get_data()
    selected_interval = data.get("interval_minutes")
    await call.message.edit_text(
        "⏱ Интервал проверки доступности:",
        reply_markup=avail_interval_kb(selected_interval),
    )


# ── FSM step 5: interval ──────────────────────────────────────────────────────

@router.callback_query(AvailGroupFSM.choosing_interval, F.data.regexp(r"^avail:interval:\d+$"))
async def avail_interval_chosen(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    n = int(call.data.split(":")[2])
    await state.update_data(interval_minutes=n)
    await state.set_state(AvailGroupFSM.confirming)
    await _show_confirm(call, state)


async def _show_confirm(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    panel_tag: str = data.get("panel_tag", "?")
    selected_tag: str = data.get("selected_tag", "?")
    sets_info: list[dict] = data.get("sets_info", [])
    selected_ids: list[int] = data.get("selected_set_ids", [])
    managed_pool_id: int | None = data.get("managed_pool_id")
    distribution: str = data.get("distribution", "same")
    interval: int = data.get("interval_minutes", 15)

    dist_label = "🔢 Всем одинаковый IP" if distribution == "same" else "🔀 Каждому свой IP"

    if managed_pool_id:
        source_section = f"Источник IP:\n  • Управляемый пул ID {managed_pool_id}"
    else:
        selected_sets = [s for s in sets_info if s["id"] in selected_ids]
        sets_label = "\n".join(
            f"  • {s['tag']} ({s['count']:,} записей)" for s in selected_sets
        ) or "  —"
        source_section = f"Наборы IP:\n{sets_label}"

    await call.message.edit_text(
        f"✅ <b>Готово к сохранению</b>\n\n"
        f"Панель: <b>{panel_tag}</b>\n"
        f"Тег хостов: <b>{selected_tag}</b>\n"
        f"{source_section}\n"
        f"Режим: {dist_label}\n"
        f"Интервал: каждые <b>{interval} мин</b>",
        reply_markup=avail_confirm_kb(),
        parse_mode="HTML",
    )


# ── FSM step 6: save ──────────────────────────────────────────────────────────

@router.callback_query(AvailGroupFSM.confirming, F.data == "avail:confirm_create")
async def avail_confirm_create(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
    db_user: User,
) -> None:
    await call.answer()
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    selected_tag: str = data["selected_tag"]
    selected_set_ids: list[int] = data.get("selected_set_ids", [])
    distribution: str = data.get("distribution", "same")
    interval_minutes: int = data.get("interval_minutes", 15)

    managed_pool_id: int | None = data.get("managed_pool_id")
    if not managed_pool_id and not selected_set_ids:
        await call.answer("Не выбраны наборы IP и не выбран пул.", show_alert=True)
        return

    auto_svc = AutomationService(session)
    group = await auto_svc.add_group(
        org_id=active_org.id,
        panel_id=panel_id,
        host_tag=selected_tag,
        ip_set_ids=selected_set_ids,
        distribution=distribution,
        interval_minutes=interval_minutes,
        managed_pool_id=managed_pool_id,
    )
    await state.clear()

    # Fire initial check immediately — don't wait for the scheduled interval
    await run_group_check_now(call.bot, group.id)

    await call.answer(
        "✅ Группа создана! Запускаю первичную проверку хостов...",
        show_alert=False,
    )
    send_audit(
        call.bot, active_org.id, db_user,
        f"Создал группу автоматизации: тег «{selected_tag}», "
        f"интервал {interval_minutes} мин, режим «{distribution}»",
    )
    await _show_groups_list(call, session, active_org)


# ── FSM back navigation ───────────────────────────────────────────────────────

@router.callback_query(F.data == "avail:back")
async def avail_back(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    current = await state.get_state()
    data = await state.get_data()

    if current is None or current == AvailGroupFSM.choosing_panel:
        # Nothing in progress — go to groups list
        await state.clear()
        await _show_groups_list(call, session, active_org)

    elif current == AvailGroupFSM.choosing_tag:
        # Back to panel selection
        rw_svc = RemnaWaveService(session)
        panels = await rw_svc.get_org_panels(active_org.id)
        await state.set_state(AvailGroupFSM.choosing_panel)
        await call.message.edit_text(
            "Выберите панель Remnawave:",
            reply_markup=avail_panels_kb(panels),
        )

    elif current == AvailGroupFSM.choosing_source_type:
        # Back to tag selection
        tags: list[str] = data.get("tags", [])
        panel_tag: str = data.get("panel_tag", "?")
        await state.set_state(AvailGroupFSM.choosing_tag)
        await call.message.edit_text(
            f"Панель: <b>{panel_tag}</b>\n\nВыберите тег хостов для мониторинга:",
            reply_markup=avail_tags_kb(tags),
            parse_mode="HTML",
        )

    elif current == AvailGroupFSM.choosing_ip_sets:
        # Back to source type selection
        panel_tag = data.get("panel_tag", "?")
        selected_tag = data.get("selected_tag", "?")
        await state.set_state(AvailGroupFSM.choosing_source_type)
        await call.message.edit_text(
            f"Панель: <b>{panel_tag}</b>  |  Тег: <b>{selected_tag}</b>\n\n"
            "Источник IP-адресов для замен:",
            reply_markup=avail_source_type_kb(),
            parse_mode="HTML",
        )

    elif current == AvailGroupFSM.choosing_pool:
        # Back to source type selection
        panel_tag = data.get("panel_tag", "?")
        selected_tag = data.get("selected_tag", "?")
        await state.set_state(AvailGroupFSM.choosing_source_type)
        await call.message.edit_text(
            f"Панель: <b>{panel_tag}</b>  |  Тег: <b>{selected_tag}</b>\n\n"
            "Источник IP-адресов для замен:",
            reply_markup=avail_source_type_kb(),
            parse_mode="HTML",
        )

    elif current == AvailGroupFSM.choosing_distribution:
        # Back to IP sets / pool selection depending on what was chosen
        managed_pool_id: int | None = data.get("managed_pool_id")
        if managed_pool_id:
            # Go back to pool picker
            pool_svc = ManagedPoolService(session)
            pools = await pool_svc.get_org_pools(active_org.id)
            await state.set_state(AvailGroupFSM.choosing_pool)
            await call.message.edit_text(
                "Выберите управляемый пул:",
                reply_markup=avail_pools_kb(pools),
            )
        else:
            sets_info: list[dict] = data.get("sets_info", [])
            selected_ids: list[int] = data.get("selected_set_ids", [])
            panel_tag = data.get("panel_tag", "?")
            selected_tag = data.get("selected_tag", "?")
            await state.set_state(AvailGroupFSM.choosing_ip_sets)
            await call.message.edit_text(
                f"Панель: <b>{panel_tag}</b>  |  Тег: <b>{selected_tag}</b>\n\n"
                "Выберите один или несколько наборов IP:",
                reply_markup=avail_ip_sets_kb(sets_info, selected_ids),
                parse_mode="HTML",
            )

    elif current == AvailGroupFSM.choosing_interval:
        # Back to distribution
        await state.set_state(AvailGroupFSM.choosing_distribution)
        await call.message.edit_text(
            "Как заменять IP хостам?",
            reply_markup=avail_distribution_kb(),
        )

    elif current == AvailGroupFSM.confirming:
        # Back to interval picker
        selected_interval = data.get("interval_minutes")
        await state.set_state(AvailGroupFSM.choosing_interval)
        await call.message.edit_text(
            "⏱ Интервал проверки доступности:",
            reply_markup=avail_interval_kb(selected_interval),
        )

    else:
        # Fallback — main menu
        await state.clear()
        await call.message.edit_text("Вы в главном меню", reply_markup=main_menu_kb())
