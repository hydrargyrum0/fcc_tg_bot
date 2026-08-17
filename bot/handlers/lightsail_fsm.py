"""Handlers for Lightsail automatic IP search.

UI flow:
  menu:ipsearch → ls:lightsail → ls:account:{id} → ls:region:{account_id}:{region}
    → ls:start / ls:pause / ls:stop
    → ls:nodes / ls:target / ls:recheck (settings)
    → ls:refresh (reload detail)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    ipsearch_services_kb,
    ls_accounts_kb,
    ls_nodes_kb,
    ls_region_detail_kb,
    ls_regions_kb,
)
from bot.states.lightsail import LightsailFSM
from db.models.organization import Organization
from services.aws_service import AWSService
from services.lightsail_api_service import get_regions
from services.lightsail_search_service import LightsailSearchService
from services.lightsail_searcher import is_running, start_search, stop_search
from services.pingachock_api_service import PingachockAPIError, get_nodes
from services.pingachock_service import PingachockService

logger = logging.getLogger(__name__)
router = Router()

_STATUS_LABEL = {
    "searching":  "🔴 Поиск...",
    "paused":     "🟡 Пауза",
    "monitoring": "🟢 Мониторинг",
    "idle":       "⚪ Ожидание",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _elapsed_str(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        secs = 0
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}ч {m}м {s}с"
    return f"{m}м {s}с" if m else f"{s}с"


async def _get_region_detail_text(
    cfg,
    working_ips: list,
    all_ips: list,
    account_tag: str,
) -> str:
    non_working = [ip for ip in all_ips if ip.is_working is False]
    pending = [ip for ip in all_ips if ip.is_working is None]

    lines = [
        f"📍 <b>{cfg.region_display_name or cfg.region}</b>",
        f"🔶 Amazon Lightsail | Аккаунт: <b>{account_tag}</b>",
        "",
        f"Статус: {_STATUS_LABEL.get(cfg.status, cfg.status)}",
        f"Прошло: {_elapsed_str(cfg.search_started_at)}",
        f"Перебрано: {cfg.total_checked}",
        f"Рабочих: {len(working_ips)} / {cfg.target_count}",
        f"Не рабочих (в ротации): {len(non_working)}",
    ]

    if pending:
        lines.append(f"Тестируется сейчас: {pending[0].ip_address}")

    lines += [
        "",
        f"🎯 Цель: {cfg.target_count} адреса",
        f"⏱ Перепроверка: {cfg.recheck_minutes} мин",
    ]

    if working_ips:
        lines += ["", "✅ <b>Рабочие адреса:</b>"]
        for ip in working_ips:
            lines.append(f"• <code>{ip.ip_address}</code>")
    else:
        lines += ["", "Рабочих адресов пока нет."]

    return "\n".join(lines)


# ── entry ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:ipsearch")
async def ipsearch_menu(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "🔍 <b>Автопоиск IP</b>\n\nВыберите сервис:",
        reply_markup=ipsearch_services_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "ls:back_services")
async def back_to_services(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "🔍 <b>Автопоиск IP</b>\n\nВыберите сервис:",
        reply_markup=ipsearch_services_kb(),
        parse_mode="HTML",
    )


# ── account selection ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "ls:lightsail")
async def ls_lightsail(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    accounts = await AWSService(session).get_org_accounts(active_org.id)
    if not accounts:
        await call.message.edit_text(
            "🔶 <b>Amazon Lightsail</b>\n\n"
            "Нет подключённых AWS-аккаунтов.\n"
            "Добавьте аккаунт в настройках.",
            reply_markup=ls_accounts_kb([]),
            parse_mode="HTML",
        )
        return
    await call.message.edit_text(
        "🔶 <b>Amazon Lightsail</b>\n\nВыберите AWS-аккаунт:",
        reply_markup=ls_accounts_kb(accounts),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^ls:account_back:\d+$"))
async def ls_account_back(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    accounts = await AWSService(session).get_org_accounts(active_org.id)
    await call.message.edit_text(
        "🔶 <b>Amazon Lightsail</b>\n\nВыберите AWS-аккаунт:",
        reply_markup=ls_accounts_kb(accounts),
        parse_mode="HTML",
    )


# ── region list ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^ls:account:\d+$"))
async def ls_account_regions(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    account_id = int(call.data.split(":")[2])
    aws_svc = AWSService(session)
    account = await aws_svc.get_account_by_id(account_id, active_org.id)
    if not account:
        await call.answer("Аккаунт не найден.", show_alert=True)
        return

    await call.message.edit_text(f"⏳ Загружаю регионы Lightsail для «{account.tag}»...")
    try:
        regions = await get_regions(account.access_key_id, account.secret_access_key)
    except Exception as e:
        await call.answer(f"❌ Ошибка AWS: {str(e)[:200]}", show_alert=True)
        accounts = await aws_svc.get_org_accounts(active_org.id)
        await call.message.edit_text(
            "🔶 <b>Amazon Lightsail</b>\n\nВыберите AWS-аккаунт:",
            reply_markup=ls_accounts_kb(accounts),
            parse_mode="HTML",
        )
        return

    svc = LightsailSearchService(session)
    all_configs = await svc.get_account_configs(account_id)
    configs_map = {c.region: c for c in all_configs}

    # Count working IPs per region
    working_counts: dict[str, int] = {}
    for cfg in all_configs:
        working = await svc.get_working_ips(cfg.id)
        working_counts[cfg.region] = len(working)

    await call.message.edit_text(
        f"🔶 <b>Amazon Lightsail</b> — <b>{account.tag}</b>\n\n"
        f"Регионов: {len(regions)}\n"
        "🔴 поиск · 🟡 пауза · 🟢 мониторинг · ⚪ не настроен",
        reply_markup=ls_regions_kb(account_id, regions, configs_map, working_counts),
        parse_mode="HTML",
    )


# ── region detail ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^ls:region:\d+:.+$"))
async def ls_region_detail(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    parts = call.data.split(":", 3)   # ls:region:{account_id}:{region}
    account_id = int(parts[2])
    region = parts[3]

    aws_svc = AWSService(session)
    account = await aws_svc.get_account_by_id(account_id, active_org.id)
    if not account:
        await call.answer("Аккаунт не найден.", show_alert=True)
        return

    svc = LightsailSearchService(session)

    # Fetch region display name from regions list (cached in DB; first visit: use API)
    cfg = await svc.get_config(account_id, region)
    if not cfg:
        # First time visiting this region: create config + fetch display name
        try:
            regions = await get_regions(account.access_key_id, account.secret_access_key)
            display_name = next(
                (r["displayName"] for r in regions if r["name"] == region), region
            )
        except Exception:
            display_name = region
        cfg = await svc.upsert_config(active_org.id, account_id, region, display_name)

    working_ips = await svc.get_working_ips(cfg.id)
    all_ips = await svc.get_all_ips(cfg.id)
    text = await _get_region_detail_text(cfg, working_ips, all_ips, account.tag)

    await call.message.edit_text(
        text,
        reply_markup=ls_region_detail_kb(cfg.id, cfg.status, is_running(cfg.id)),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^ls:refresh:\d+$"))
async def ls_refresh(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer("🔄")
    config_id = int(call.data.split(":")[2])
    svc = LightsailSearchService(session)
    cfg = await svc.get_config_by_id(config_id)
    if not cfg:
        await call.answer("Конфиг не найден.", show_alert=True)
        return

    account = await AWSService(session).get_account_by_id(cfg.aws_account_id, active_org.id)
    working_ips = await svc.get_working_ips(config_id)
    all_ips = await svc.get_all_ips(config_id)
    text = await _get_region_detail_text(
        cfg, working_ips, all_ips, account.tag if account else "?"
    )

    try:
        await call.message.edit_text(
            text,
            reply_markup=ls_region_detail_kb(cfg.id, cfg.status, is_running(cfg.id)),
            parse_mode="HTML",
        )
    except Exception:
        pass  # message not modified


@router.callback_query(F.data.regexp(r"^ls:back_regions:\d+$"))
async def ls_back_to_regions(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    config_id = int(call.data.split(":")[2])
    svc = LightsailSearchService(session)
    cfg = await svc.get_config_by_id(config_id)
    if not cfg:
        await call.answer("Конфиг не найден.", show_alert=True)
        return

    aws_svc = AWSService(session)
    account = await aws_svc.get_account_by_id(cfg.aws_account_id, active_org.id)
    if not account:
        await call.answer("Аккаунт не найден.", show_alert=True)
        return

    all_configs = await svc.get_account_configs(cfg.aws_account_id)
    configs_map = {c.region: c for c in all_configs}
    working_counts: dict[str, int] = {}
    for c in all_configs:
        wips = await svc.get_working_ips(c.id)
        working_counts[c.region] = len(wips)

    try:
        regions = await get_regions(account.access_key_id, account.secret_access_key)
    except Exception:
        regions = [{"name": c.region, "displayName": c.region_display_name} for c in all_configs]

    await call.message.edit_text(
        f"🔶 <b>Amazon Lightsail</b> — <b>{account.tag}</b>\n\n"
        f"Регионов: {len(regions)}\n"
        "🔴 поиск · 🟡 пауза · 🟢 мониторинг · ⚪ не настроен",
        reply_markup=ls_regions_kb(cfg.aws_account_id, regions, configs_map, working_counts),
        parse_mode="HTML",
    )


# ── search controls ───────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^ls:start:\d+$"))
async def ls_start(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    config_id = int(call.data.split(":")[2])
    svc = LightsailSearchService(session)
    cfg = await svc.get_config_by_id(config_id)
    if not cfg:
        await call.answer("Конфиг не найден.", show_alert=True)
        return

    if is_running(config_id):
        await call.answer("Поиск уже запущен.", show_alert=True)
        return

    # Check Pingachock configured
    pc = await PingachockService(session).get_settings(cfg.org_id)
    if not pc:
        await call.answer("❌ Pingachock не настроен для этой организации.", show_alert=True)
        return

    await svc.set_status(config_id, "searching")
    await start_search(config_id)

    account = await AWSService(session).get_account_by_id(cfg.aws_account_id, active_org.id)
    working_ips = await svc.get_working_ips(config_id)
    all_ips = await svc.get_all_ips(config_id)
    cfg = await svc.get_config_by_id(config_id)  # reload
    text = await _get_region_detail_text(
        cfg, working_ips, all_ips, account.tag if account else "?"
    )
    await call.message.edit_text(
        text,
        reply_markup=ls_region_detail_kb(config_id, cfg.status, is_running(config_id)),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^ls:pause:\d+$"))
async def ls_pause(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    config_id = int(call.data.split(":")[2])
    await stop_search(config_id)  # stop_search → marks as paused + deletes instance

    svc = LightsailSearchService(session)
    cfg = await svc.get_config_by_id(config_id)
    if not cfg:
        return
    account = await AWSService(session).get_account_by_id(cfg.aws_account_id, active_org.id)
    working_ips = await svc.get_working_ips(config_id)
    all_ips = await svc.get_all_ips(config_id)
    text = await _get_region_detail_text(
        cfg, working_ips, all_ips, account.tag if account else "?"
    )
    await call.message.edit_text(
        text,
        reply_markup=ls_region_detail_kb(config_id, cfg.status, False),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^ls:stop:\d+$"))
async def ls_stop(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    config_id = int(call.data.split(":")[2])

    # Stop the task + delete instance (same as pause for now)
    await stop_search(config_id)

    # Additionally: release all non-working static IPs from AWS
    svc = LightsailSearchService(session)
    cfg = await svc.get_config_by_id(config_id)
    if not cfg:
        return
    account = await AWSService(session).get_account_by_id(cfg.aws_account_id, active_org.id)
    if account:
        all_ips = await svc.get_all_ips(config_id)
        from services.lightsail_api_service import detach_static_ip, release_static_ip
        for ip in all_ips:
            if ip.is_working is False:
                try:
                    await detach_static_ip(
                        cfg.region, account.access_key_id, account.secret_access_key,
                        ip.static_ip_name,
                    )
                    await release_static_ip(
                        cfg.region, account.access_key_id, account.secret_access_key,
                        ip.static_ip_name,
                    )
                except Exception as e:
                    logger.warning("ls_stop: could not release %s: %s", ip.static_ip_name, e)
                await svc.delete_static_ip(ip.static_ip_name)

    await svc.set_status(config_id, "idle")
    cfg = await svc.get_config_by_id(config_id)
    working_ips = await svc.get_working_ips(config_id)
    all_ips = await svc.get_all_ips(config_id)
    text = await _get_region_detail_text(
        cfg, working_ips, all_ips, account.tag if account else "?"
    )
    await call.message.edit_text(
        text,
        reply_markup=ls_region_detail_kb(config_id, cfg.status, False),
        parse_mode="HTML",
    )


# ── settings: target count ────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^ls:target:\d+$"))
async def ls_target_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    config_id = int(call.data.split(":")[2])
    await state.set_state(LightsailFSM.editing_target)
    await state.update_data(ls_config_id=config_id)
    await call.message.edit_text(
        "🎯 <b>Цель</b>\n\n"
        "Введите количество рабочих IP, которое нужно найти (1–5):\n"
        "<i>После достижения цели поиск автоматически останавливается.</i>",
        parse_mode="HTML",
    )


@router.message(LightsailFSM.editing_target)
async def ls_target_input(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not (1 <= int(text) <= 5):
        await message.answer("❌ Введите число от 1 до 5:")
        return
    data = await state.get_data()
    config_id = data["ls_config_id"]
    await LightsailSearchService(session).update_target(config_id, int(text))
    await state.clear()
    await message.answer(f"✅ Цель установлена: <b>{text}</b>", parse_mode="HTML")
    # Reload detail
    svc = LightsailSearchService(session)
    cfg = await svc.get_config_by_id(config_id)
    if cfg:
        from services.aws_service import AWSService as _AWSService
        from db.models.organization import Organization as _Org
        account = await _AWSService(session).get_account_by_id(cfg.aws_account_id, cfg.org_id)
        working_ips = await svc.get_working_ips(config_id)
        all_ips = await svc.get_all_ips(config_id)
        detail = await _get_region_detail_text(
            cfg, working_ips, all_ips, account.tag if account else "?"
        )
        await message.answer(
            detail,
            reply_markup=ls_region_detail_kb(config_id, cfg.status, is_running(config_id)),
            parse_mode="HTML",
        )


# ── settings: recheck interval ────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^ls:recheck:\d+$"))
async def ls_recheck_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    config_id = int(call.data.split(":")[2])
    await state.set_state(LightsailFSM.editing_recheck)
    await state.update_data(ls_config_id=config_id)
    await call.message.edit_text(
        "⏱ <b>Интервал перепроверки</b>\n\n"
        "Введите интервал в минутах (минимум 10):\n"
        "<i>Раз в этот период бот проверит найденные адреса и при необходимости заменит упавшие.</i>",
        parse_mode="HTML",
    )


@router.message(LightsailFSM.editing_recheck)
async def ls_recheck_input(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) < 10:
        await message.answer("❌ Введите число ≥ 10 (минут):")
        return
    data = await state.get_data()
    config_id = data["ls_config_id"]
    await LightsailSearchService(session).update_recheck(config_id, int(text))
    await state.clear()
    await message.answer(f"✅ Перепроверка каждые <b>{text} мин</b>", parse_mode="HTML")
    svc = LightsailSearchService(session)
    cfg = await svc.get_config_by_id(config_id)
    if cfg:
        account = await AWSService(session).get_account_by_id(cfg.aws_account_id, cfg.org_id)
        working_ips = await svc.get_working_ips(config_id)
        all_ips = await svc.get_all_ips(config_id)
        detail = await _get_region_detail_text(
            cfg, working_ips, all_ips, account.tag if account else "?"
        )
        await message.answer(
            detail,
            reply_markup=ls_region_detail_kb(config_id, cfg.status, is_running(config_id)),
            parse_mode="HTML",
        )


# ── settings: Pingachock nodes ────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^ls:nodes:\d+$"))
async def ls_nodes_menu(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    config_id = int(call.data.split(":")[2])

    pc = await PingachockService(session).get_settings(active_org.id)
    if not pc:
        await call.answer("Pingachock не настроен.", show_alert=True)
        return

    await call.message.edit_text("⏳ Загружаю узлы Pingachock...")
    try:
        nodes = await get_nodes(pc.api_url, pc.api_key)
    except Exception as e:
        await call.answer(f"❌ {str(e)[:200]}", show_alert=True)
        return

    nodes = [n for n in nodes if not n.get("is_virtual", False)]

    svc = LightsailSearchService(session)
    cfg = await svc.get_config_by_id(config_id)
    current_ids: list[str] = list(cfg.node_ids or []) if cfg else []

    await state.set_state(LightsailFSM.selecting_nodes)
    await state.update_data(ls_config_id=config_id, nodes=nodes, selected_ids=current_ids)

    selected_label = f"{len(current_ids)} узл(а)" if current_ids else "все"
    await call.message.edit_text(
        f"📡 <b>Узлы Pingachock для поиска</b>\n\n"
        f"Выбрано: <b>{selected_label}</b>\n"
        "Только отмеченные узлы будут проверять IP-адреса.",
        reply_markup=ls_nodes_kb(config_id, nodes, set(current_ids)),
        parse_mode="HTML",
    )


@router.callback_query(LightsailFSM.selecting_nodes, F.data.regexp(r"^ls:node_toggle:\d+:.+$"))
async def ls_node_toggle(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    parts = call.data.split(":", 3)   # ls:node_toggle:{config_id}:{node_id}
    config_id = int(parts[2])
    node_id = parts[3]

    data = await state.get_data()
    nodes: list[dict] = data.get("nodes", [])
    selected: list[str] = list(data.get("selected_ids", []))

    if node_id in selected:
        selected.remove(node_id)
    else:
        selected.append(node_id)

    await state.update_data(selected_ids=selected)
    label = f"{len(selected)} узл(а)" if selected else "все"
    await call.message.edit_text(
        f"📡 <b>Узлы Pingachock для поиска</b>\n\n"
        f"Выбрано: <b>{label}</b>\n"
        "Только отмеченные узлы будут проверять IP-адреса.",
        reply_markup=ls_nodes_kb(config_id, nodes, set(selected)),
        parse_mode="HTML",
    )


@router.callback_query(LightsailFSM.selecting_nodes, F.data.regexp(r"^ls:nodes_all:\d+$"))
async def ls_nodes_all(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await call.answer()
    data = await state.get_data()
    config_id: int = data.get("ls_config_id", int(call.data.split(":")[2]))
    await LightsailSearchService(session).update_node_ids(config_id, None)
    await state.clear()
    await call.message.edit_text(
        "✅ Фильтр узлов снят — используются <b>все узлы</b>.",
        parse_mode="HTML",
    )


@router.callback_query(LightsailFSM.selecting_nodes, F.data.regexp(r"^ls:nodes_save:\d+$"))
async def ls_nodes_save(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await call.answer()
    data = await state.get_data()
    config_id: int = data.get("ls_config_id", int(call.data.split(":")[2]))
    selected: list[str] = data.get("selected_ids", [])
    await LightsailSearchService(session).update_node_ids(config_id, selected or None)
    await state.clear()
    label = f"{len(selected)} узл(а)" if selected else "все"
    await call.message.edit_text(
        f"✅ Сохранено. Узлов для поиска: <b>{label}</b>.",
        parse_mode="HTML",
    )
