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
from services.ip_check_service import CHECK_BATCH, distributed_ping_check, expand_addresses
from services.ip_set_service import IpSetService
from services.pingachock_api_service import PingachockAPIError
from services.pingachock_service import PingachockService
from services.remnawave_api_service import RemnaWaveAPIError, get_hosts, update_host_address
from services.remnawave_service import RemnaWaveService

logger = logging.getLogger(__name__)

# ── shared state (module-level, single event-loop safe) ───────────────────────

_processing_groups: set[int] = set()  # group IDs currently in-flight
_cancel_flags: set[int] = set()        # group IDs with cancel requested

# Per-group cooldown for recently-confirmed-bad IPs: {group_id: {ip: monotonic_time}}
# Prevents cycling between two bad IPs that happen to pass BoT5 as replacement candidates.
_recently_failed: dict[int, dict[str, float]] = {}
FAILED_IP_COOLDOWN = 1800  # 30 minutes — don't reuse a confirmed-bad IP as replacement

POLL_INTERVAL = 60       # seconds between "which groups are due?" wakeups
LOSS_THRESHOLD = 0.25    # >1 packet out of 4 lost → IP is "lossy", find better


def request_skip(group_id: int) -> None:
    """Called from the bot cancel-button handler to stop a running search."""
    _cancel_flags.add(group_id)


def _mark_ips_failed(group_id: int, ips: set[str]) -> None:
    """Record IPs that just failed BoT5 — exclude them from replacement search for FAILED_IP_COOLDOWN seconds."""
    bucket = _recently_failed.setdefault(group_id, {})
    now = time.monotonic()
    for ip in ips:
        bucket[ip] = now
    logger.debug("Group %d: marked %d IPs in failed-cooldown: %s", group_id, len(ips), ips)


def _get_cooldown_ips(group_id: int) -> frozenset[str]:
    """Return IPs still within their cooldown window for this group."""
    bucket = _recently_failed.get(group_id)
    if not bucket:
        return frozenset()
    now = time.monotonic()
    expired = [ip for ip, t in bucket.items() if now - t > FAILED_IP_COOLDOWN]
    for ip in expired:
        del bucket[ip]
    return frozenset(bucket)


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
        logger.info("Group %d: no hosts with tag '%s'", group_id, group.host_tag)
        return

    # ── build IP pool from the group's IP sets ───────────────────────────────
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
    logger.info(
        "Group %d [%s]: %d hosts, pool %d IPs from [%s]",
        group_id, group.host_tag, len(tagged_hosts), len(pool), set_names,
    )

    # ── check current IPs via Pingachock ────────────────────────────────────
    unique_current_ips = list({h.get("address", "") for h in tagged_hosts if h.get("address")})
    if not unique_current_ips:
        return

    logger.info("Group %d: checking current IPs (Best of 5): %s", group_id, unique_current_ips)
    try:
        ip_results = await _check_best_of_5(pc.api_url, pc.api_key, unique_current_ips)
    except PingachockAPIError as e:
        logger.error("Group %d: Pingachock check failed: %s", group_id, e)
        return

    # Classify each IP: dead / lossy / ok
    dead_ips: dict[str, float] = {}    # ip → loss_pct (1.0)
    lossy_ips: dict[str, float] = {}   # ip → loss_pct (>LOSS_THRESHOLD)
    for ip, (reachable, loss_pct) in ip_results.items():
        if not reachable:
            dead_ips[ip] = loss_pct
        elif loss_pct > LOSS_THRESHOLD:
            lossy_ips[ip] = loss_pct

    log_parts = {
        ip: f"{'DEAD' if not r else ('LOSSY' if l > LOSS_THRESHOLD else 'OK')} loss={l*100:.0f}%"
        for ip, (r, l) in ip_results.items()
    }
    logger.info("Group %d: quality results: %s", group_id, log_parts)

    if not dead_ips and not lossy_ips:
        logger.info("Group %d: all IPs OK (loss ≤ 25%%)", group_id)
        return

    if dead_ips:
        logger.info("Group %d: dead IPs: %s", group_id, list(dead_ips))
    if lossy_ips:
        logger.info("Group %d: lossy IPs (>1/4 loss): %s", group_id, {
            ip: f"{l*100:.0f}%" for ip, l in lossy_ips.items()
        })

    # All bad IPs go into cooldown — prevents cycling between bad addresses
    _mark_ips_failed(group_id, set(dead_ips) | set(lossy_ips))

    all_bad_ips = {**dead_ips, **lossy_ips}

    # ── fix bad IPs according to distribution mode ───────────────────────────
    if group.distribution == "same":
        # All hosts share one IP — treat the whole group as a unit.
        bad_ip = next(iter(all_bad_ips))
        is_dead = bad_ip in dead_ips
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
            reason="dead" if is_dead else "lossy",
            loss_pct=all_bad_ips[bad_ip],
        )
    else:
        # Each host may have its own IP — process bad ones sequentially.
        for host in tagged_hosts:
            if group.id in _cancel_flags:
                _cancel_flags.discard(group.id)
                break
            host_ip = host.get("address", "")
            if host_ip not in all_bad_ips:
                continue
            label = (host.get("remark") or host.get("uuid", ""))[:60]
            is_dead = host_ip in dead_ips
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
                reason="dead" if is_dead else "lossy",
                loss_pct=all_bad_ips[host_ip],
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
    reason: str = "dead",    # "dead" — недоступен; "lossy" — высокие потери
    loss_pct: float = 1.0,
) -> None:
    """Send live alert, search for replacement, apply, edit alert with result."""
    start_t = time.monotonic()

    def _elapsed_str() -> str:
        secs = int(time.monotonic() - start_t)
        m, s = divmod(secs, 60)
        return f"{m}м {s}с" if m else f"{s}с"

    if reason == "lossy":
        # >1/4 packets lost — show actual loss as N/4
        loss_count = round(loss_pct * 4)
        alert_base = (
            f"📉 <b>Высокие потери пакетов</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — потери {loss_count}/4 пакетов\n"
            f"Ищем замену с лучшим качеством из: <b>{set_names}</b>"
        )
    else:
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
    # Exclude:
    #   1. The current bad host IPs (prevents "X → X" self-replacement).
    #      Strip port suffix: Remnawave may return "1.2.3.4:443", pool has bare IPs.
    #   2. IPs confirmed bad in recent cycles (prevents A↔B cycling between two bad IPs).
    bad_ips_to_skip = frozenset(
        h.get("address", "").split(":")[0]
        for h in hosts_to_fix
        if h.get("address")
    )
    cooldown_ips = _get_cooldown_ips(group.id)
    exclude_for_replacement = bad_ips_to_skip | cooldown_ips
    if cooldown_ips:
        logger.info(
            "Group %d: also excluding %d cooldown IPs from replacement search: %s",
            group.id, len(cooldown_ips), cooldown_ips,
        )

    # dead → accept any reachable (even with losses); lossy → must have low loss
    require_low_loss = (reason == "lossy")

    new_ip: str | None = None
    cancelled = False
    try:
        new_ip = await _find_replacement(
            pc.api_url, pc.api_key, pool, group.id,
            exclude_ips=exclude_for_replacement,
            require_low_loss=require_low_loss,
        )
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
        if reason == "lossy":
            result_text = (
                f"✅ <b>Адрес улучшен</b>\n\n"
                f"Хост/группа: <b>{host_label}</b>\n"
                f"<code>{display_bad_ip}</code> → <code>{new_ip}</code>\n"
                f"Потери устранены\n\n"
                f"Время поиска: {elapsed}"
            )
        else:
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
    elif reason == "lossy":
        result_text = (
            f"ℹ️ <b>Замена не найдена</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> остаётся — лучший адрес не найден\n\n"
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


async def _check_best_of_5(
    api_url: str,
    api_key: str,
    ips: list[str],
) -> dict[str, tuple[bool, float]]:
    """Sequential Best-of-5 with packet-loss tracking.

    Runs up to 5 ICMP-ping rounds (4 packets each). Only undecided IPs are
    included in each round; an IP is decided when it reaches ≥3 OK or ≥3 FAIL
    votes. Packet counts are accumulated across all OK rounds.

    Returns {ip: (reachable, loss_pct)} where:
      reachable — True if ≥3 rounds reported the IP as reachable
      loss_pct  — fraction of packets lost (0.0–1.0) across reachable rounds;
                  1.0 if unreachable or no packet data available
    """
    ok_votes: dict[str, int] = {ip: 0 for ip in ips}
    fail_votes: dict[str, int] = {ip: 0 for ip in ips}
    total_sent: dict[str, int] = {ip: 0 for ip in ips}
    total_recv: dict[str, int] = {ip: 0 for ip in ips}

    for round_num in range(1, 6):
        undecided = [ip for ip in ips if ok_votes[ip] < 3 and fail_votes[ip] < 3]
        if not undecided:
            logger.debug("Bo5: all %d IPs decided after round %d", len(ips), round_num - 1)
            break

        logger.debug("Bo5 round %d/5: checking %d IPs: %s", round_num, len(undecided), undecided)
        try:
            ping_results = await distributed_ping_check(api_url, api_key, undecided)
        except PingachockAPIError as e:
            logger.warning("Bo5 round %d: Pingachock error — counting as FAIL: %s", round_num, e)
            for ip in undecided:
                fail_votes[ip] += 1
            continue

        for ip in undecided:
            reachable, recv, sent = ping_results.get(ip, (False, 0, 4))
            if reachable:
                ok_votes[ip] += 1
                total_sent[ip] += sent
                total_recv[ip] += recv
            else:
                fail_votes[ip] += 1

        tally = {}
        for ip in ips:
            if total_sent[ip] > 0:
                loss = 1.0 - total_recv[ip] / total_sent[ip]
                tally[ip] = f"{ok_votes[ip]}✓/{fail_votes[ip]}✗ loss={loss*100:.0f}%"
            else:
                tally[ip] = f"{ok_votes[ip]}✓/{fail_votes[ip]}✗"
        logger.info("Bo5 round %d: %s", round_num, tally)

    results: dict[str, tuple[bool, float]] = {}
    for ip in ips:
        reachable = ok_votes[ip] >= 3
        if reachable and total_sent[ip] > 0:
            loss_pct = 1.0 - total_recv[ip] / total_sent[ip]
        else:
            loss_pct = 1.0 if not reachable else 0.0
        results[ip] = (reachable, loss_pct)
    return results


async def _find_replacement(
    api_url: str,
    api_key: str,
    pool: list[str],
    group_id: int,
    exclude_ips: frozenset[str] = frozenset(),
    require_low_loss: bool = False,
) -> str | None:
    """Scan pool in batches, verify each candidate via BoT5 ICMP ping.

    require_low_loss=False (dead IP): accept any reachable IP.
    require_low_loss=True  (lossy IP): only accept IPs with loss ≤ LOSS_THRESHOLD.

    exclude_ips: always skipped (current bad IPs + cooldown cache).
    Returns first qualifying IP or None if pool exhausted / cancelled.
    """
    offset = 0
    while offset < len(pool):
        if group_id in _cancel_flags:
            return None
        batch = [ip for ip in pool[offset: offset + CHECK_BATCH] if ip not in exclude_ips]
        offset += CHECK_BATCH
        if not batch:
            continue
        try:
            results = await _check_best_of_5(api_url, api_key, batch)
        except Exception as e:
            logger.warning("Group %d: BoT5 error during replacement search: %s", group_id, e)
            continue
        for ip in batch:
            reachable, loss_pct = results.get(ip, (False, 1.0))
            if not reachable:
                continue
            if require_low_loss and loss_pct > LOSS_THRESHOLD:
                logger.debug(
                    "Group %d: skip %s — reachable but loss=%.0f%% > threshold",
                    group_id, ip, loss_pct * 100,
                )
                continue
            qualifier = f"loss={loss_pct*100:.0f}%"
            logger.info("Group %d: replacement %s passed BoT5 (%s)", group_id, ip, qualifier)
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
