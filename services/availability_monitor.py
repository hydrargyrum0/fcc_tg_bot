"""Background monitor: Поддержание доступности IP.

Every POLL_INTERVAL seconds checks which AutomationGroups are due for a
check.  For each due group it fires an asyncio task that:
  1. Checks all current host IPs via Pingachock distributed check.
  2. For every bad IP: sends an alert to all org members, searches the
     IP-set pool for a working replacement (Pingachock-verified), updates
     Remnawave hosts, then edits the alert with the result.
  3. Updates last_checked_at so the next check happens at the right time.

Cancel flow:
  The alert message carries an "⏭ Пропустить проверку" button.
  Its callback (`avail:skip:{group_id}`) calls `request_skip(group_id)`.
  _find_replacement checks the flag between every Pingachock batch and
  returns None if cancelled.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models.automation_group import AutomationGroup
from db.models.organization_member import OrganizationMember
from services.automation_service import AutomationService
from services.ip_check_service import CHECK_BATCH, distributed_check, expand_addresses
from services.ip_set_service import IpSetService
from services.pingachock_api_service import PingachockAPIError
from services.pingachock_service import PingachockService
from services.remnawave_api_service import RemnaWaveAPIError, get_hosts, update_host_address
from services.remnawave_service import RemnaWaveService

logger = logging.getLogger(__name__)

# ── shared state (module-level, single event-loop safe) ───────────────────────

_processing_groups: set[int] = set()  # group IDs currently in-flight
_cancel_flags: set[int] = set()        # group IDs with cancel requested

POLL_INTERVAL = 60  # seconds between "which groups are due?" wakeups


def request_skip(group_id: int) -> None:
    """Called from the bot cancel-button handler to stop a running search."""
    _cancel_flags.add(group_id)


# ── main loop ─────────────────────────────────────────────────────────────────

async def run_availability_monitor(
    bot: Bot,
    session_factory: async_sessionmaker,
) -> None:
    """Entrypoint — run as asyncio.create_task in main()."""
    logger.info("Availability monitor started")
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            due = await _get_due_groups(session_factory)
            for group in due:
                if group.id not in _processing_groups:
                    asyncio.create_task(
                        _process_group(bot, session_factory, group.id),
                        name=f"avail-group-{group.id}",
                    )
        except Exception:
            logger.exception("Availability monitor: unexpected error in main loop")


# ── due-group detection ───────────────────────────────────────────────────────

async def _get_due_groups(session_factory: async_sessionmaker) -> list[AutomationGroup]:
    async with session_factory() as session:
        svc = AutomationService(session)
        groups = await svc.get_all_enabled()

    now = datetime.now(timezone.utc)
    due: list[AutomationGroup] = []
    for g in groups:
        if g.last_checked_at is None:
            due.append(g)
            continue
        last = g.last_checked_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now >= last + timedelta(minutes=g.interval_minutes):
            due.append(g)
    return due


# ── per-group processing ──────────────────────────────────────────────────────

async def _process_group(
    bot: Bot,
    session_factory: async_sessionmaker,
    group_id: int,
) -> None:
    _processing_groups.add(group_id)
    try:
        await _do_process_group(bot, session_factory, group_id)
    except Exception:
        logger.exception("Availability monitor: error processing group %d", group_id)
    finally:
        _processing_groups.discard(group_id)


async def _do_process_group(
    bot: Bot,
    session_factory: async_sessionmaker,
    group_id: int,
) -> None:
    # ── load everything from DB ──────────────────────────────────────────────
    async with session_factory() as session:
        result = await session.execute(
            select(AutomationGroup).where(AutomationGroup.id == group_id)
        )
        group = result.scalar_one_or_none()
        if not group or not group.enabled:
            return

        auto_svc = AutomationService(session)
        await auto_svc.mark_checked(group_id)

        rw_svc = RemnaWaveService(session)
        panel = await rw_svc.get_panel_by_id_any(group.panel_id)

        pc_svc = PingachockService(session)
        pc = await pc_svc.get_settings(group.org_id)

        ip_svc = IpSetService(session)
        sets = await ip_svc.get_sets_by_ids(list(group.ip_set_ids))

        mem_result = await session.execute(
            select(OrganizationMember.user_id).where(
                OrganizationMember.org_id == group.org_id
            )
        )
        member_ids: list[int] = [row[0] for row in mem_result.fetchall()]

    if not panel:
        logger.warning("Group %d: panel %d not found — skipping", group_id, group.panel_id)
        return
    if not pc:
        logger.warning("Group %d: Pingachock not configured for org %d — skipping", group_id, group.org_id)
        return

    # ── fetch current hosts from Remnawave ───────────────────────────────────
    try:
        all_hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        logger.error("Group %d: failed to load hosts: %s", group_id, e)
        return

    tagged_hosts = [h for h in all_hosts if group.host_tag in (h.get("tags") or [])]
    if not tagged_hosts:
        return

    # ── build IP pool from the group's IP sets (Pingachock check required) ──
    raw_pool: list[str] = []
    for s in sets:
        expanded, _ = expand_addresses(s.addresses)
        raw_pool.extend(expanded)
    seen: set[str] = set()
    pool: list[str] = []
    for ip in raw_pool:
        if ip not in seen:
            seen.add(ip)
            pool.append(ip)

    set_names = ", ".join(s.tag for s in sets) if sets else "—"

    # ── check current IPs via Pingachock ────────────────────────────────────
    unique_current_ips = list({h.get("address", "") for h in tagged_hosts if h.get("address")})
    if not unique_current_ips:
        return

    try:
        ip_status = await distributed_check(pc.api_url, pc.api_key, unique_current_ips)
    except PingachockAPIError as e:
        logger.error("Group %d: Pingachock check failed: %s", group_id, e)
        return

    bad_current_ips = {ip for ip, ok in ip_status.items() if not ok}
    if not bad_current_ips:
        return  # all good

    # ── fix bad IPs according to distribution mode ───────────────────────────
    if group.distribution == "same":
        # All hosts share one IP.  Treat the whole group as a unit.
        bad_ip = next(iter(bad_current_ips))
        await _replace_ip_for_hosts(
            bot=bot,
            group=group,
            panel=panel,
            pc=pc,
            hosts_to_fix=tagged_hosts,
            display_bad_ip=bad_ip,
            host_label=group.host_tag,
            pool=pool,
            set_names=set_names,
            member_ids=member_ids,
        )
    else:
        # Each host may have its own IP — process bad ones sequentially.
        for host in tagged_hosts:
            if group.id in _cancel_flags:
                _cancel_flags.discard(group.id)
                break
            host_ip = host.get("address", "")
            if host_ip not in bad_current_ips:
                continue
            label = (host.get("remark") or host.get("uuid", ""))[:60]
            await _replace_ip_for_hosts(
                bot=bot,
                group=group,
                panel=panel,
                pc=pc,
                hosts_to_fix=[host],
                display_bad_ip=host_ip,
                host_label=label,
                pool=pool,
                set_names=set_names,
                member_ids=member_ids,
            )


# ── replacement logic ─────────────────────────────────────────────────────────

async def _replace_ip_for_hosts(
    bot: Bot,
    group: AutomationGroup,
    panel,
    pc,
    hosts_to_fix: list[dict],
    display_bad_ip: str,
    host_label: str,
    pool: list[str],
    set_names: str,
    member_ids: list[int],
) -> None:
    """Send live alert, search for working replacement, apply, send result."""
    start_t = time.monotonic()

    def _elapsed_str() -> str:
        secs = int(time.monotonic() - start_t)
        m, s = divmod(secs, 60)
        return f"{m}м {s}с" if m else f"{s}с"

    alert_base = (
        f"⚠️ <b>Недоступный IP</b>\n\n"
        f"Хост/группа: <b>{host_label}</b>\n"
        f"IP: <code>{display_bad_ip}</code> — недоступен\n"
        f"Ищем замену из: <b>{set_names}</b>"
    )
    skip_kb = _skip_kb(group.id)

    # Send initial alert to all org members
    alert_msgs: list[tuple[int, int]] = []
    for chat_id in member_ids:
        try:
            msg = await bot.send_message(
                chat_id,
                alert_base + "\n\nПрошло: 0с",
                parse_mode="HTML",
                reply_markup=skip_kb,
            )
            alert_msgs.append((chat_id, msg.message_id))
        except Exception:
            pass

    if not alert_msgs:
        # No members to notify — still do the replacement silently
        pass

    # ── elapsed ticker ───────────────────────────────────────────────────────
    ticker_done: list[bool] = [False]

    async def _tick() -> None:
        while not ticker_done[0]:
            await asyncio.sleep(15)
            if ticker_done[0]:
                break
            new_text = alert_base + f"\n\nПрошло: {_elapsed_str()}"
            for chat_id, msg_id in alert_msgs:
                try:
                    await asyncio.wait_for(
                        bot.edit_message_text(
                            new_text,
                            chat_id=chat_id,
                            message_id=msg_id,
                            parse_mode="HTML",
                            reply_markup=skip_kb,
                        ),
                        timeout=6,
                    )
                except Exception:
                    pass

    ticker = asyncio.create_task(_tick())

    # ── find replacement (Pingachock-verified) ───────────────────────────────
    new_ip: str | None = None
    cancelled = False
    try:
        new_ip = await _find_replacement(pc.api_url, pc.api_key, pool, group.id)
        cancelled = group.id in _cancel_flags
        if cancelled:
            _cancel_flags.discard(group.id)
    finally:
        ticker_done[0] = True
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass

    elapsed = _elapsed_str()

    # ── apply replacement ────────────────────────────────────────────────────
    if new_ip and not cancelled:
        for h in hosts_to_fix:
            try:
                await update_host_address(panel.url, panel.api_token, h["uuid"], new_ip)
            except RemnaWaveAPIError as e:
                logger.error(
                    "Group %d: failed to update host %s: %s", group.id, h["uuid"], e
                )
        result_text = (
            f"✅ <b>Адрес заменён</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"<code>{display_bad_ip}</code> → <code>{new_ip}</code>\n\n"
            f"Время поиска: {elapsed}"
        )
    elif cancelled:
        result_text = (
            f"⏭ <b>Поиск отменён</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — замена не выполнена\n\n"
            f"Время поиска: {elapsed}"
        )
    else:
        result_text = (
            f"❌ <b>Замена не найдена</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — рабочих адресов нет\n\n"
            f"Время поиска: {elapsed}"
        )

    # Edit all alert messages with final result
    for chat_id, msg_id in alert_msgs:
        try:
            await bot.edit_message_text(
                result_text,
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="HTML",
            )
        except Exception:
            pass


async def _find_replacement(
    api_url: str,
    api_key: str,
    pool: list[str],
    group_id: int,
) -> str | None:
    """Scan pool in batches of CHECK_BATCH via Pingachock.

    Every IP MUST pass Pingachock check before being returned.
    Returns first confirmed-working IP or None if cancelled / exhausted.
    """
    offset = 0
    while offset < len(pool):
        if group_id in _cancel_flags:
            return None
        batch = pool[offset: offset + CHECK_BATCH]
        offset += CHECK_BATCH
        try:
            results = await distributed_check(api_url, api_key, batch)
        except PingachockAPIError as e:
            logger.warning("Group %d: Pingachock error during replacement search: %s", group_id, e)
            continue
        for ip in batch:
            if results.get(ip):
                return ip
    return None


# ── inline keyboard for skip button ──────────────────────────────────────────

def _skip_kb(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏭ Пропустить эту проверку",
            callback_data=f"avail:skip:{group_id}",
        )],
    ])
