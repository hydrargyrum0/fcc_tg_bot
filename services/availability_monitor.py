"""Background monitor: Поддержание доступности IP.

Every POLL_INTERVAL seconds checks which AutomationGroups are due for a
check.  For each due group it fires an asyncio task that:
  1. Checks all current host IPs via a single Pingachock distributed check.
  2. For every bad IP: sends an alert to all org members, searches the
     IP-set pool for a working replacement (Pingachock-verified), updates
     Remnawave hosts, then edits the alert with the result.
  3. Updates last_checked_at so the next check happens at the right time.

Checking strategy:
  A single distributed ping check is made (not Best-of-N). The result is
  trusted as-is. Loss data is included only when nodes report raw packet
  counts; nodes that don't report packets are skipped for loss calculation.

Cancel flow:
  The alert message carries an "⏭ Пропустить проверку" button.
  Its callback (`avail:skip:{group_id}`) calls `request_skip(group_id)`.
  _find_replacement checks the flag between every pool batch and
  returns (None, None, None) if cancelled.

Distribution modes:
  "same" — all hosts share one IP.  The group tag is used as the alert label.
  "each" — each host has a unique IP.  Runs silently (no Telegram alerts).
           Each bad host gets a unique replacement; all other hosts' IPs are
           excluded from the search to prevent duplicates within the group.
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
from db.models.managed_pool import ManagedIp
from db.models.organization_member import OrganizationMember
from services.automation_service import AutomationService
from services.ip_check_service import (
    CHECK_BATCH, _automation_active,
    distributed_ping_check, expand_addresses, tls_check_batch,
)
from services.ip_set_service import IpSetService
from services.managed_pool_service import ManagedPoolService
from services.pingachock_api_service import PingachockAPIError
from services.pingachock_service import PingachockService
from services.remnawave_api_service import RemnaWaveAPIError, get_hosts, update_host_address
from services.remnawave_service import RemnaWaveService

logger = logging.getLogger(__name__)

# ── module initialisation (called from main.py before tasks start) ────────────

_session_factory: "async_sessionmaker | None" = None


def init(session_factory: "async_sessionmaker") -> None:
    """Store session_factory so run_group_check_now() can use it."""
    global _session_factory
    _session_factory = session_factory


async def run_group_check_now(bot: "Bot", group_id: int) -> None:
    """Fire an immediate check for a newly created automation group.

    Safe to call from FSM handlers: creates a background asyncio task and
    returns immediately.  No-ops silently if init() was not called yet.
    """
    if _session_factory is None:
        logger.warning("run_group_check_now: session_factory not initialised (call init first)")
        return
    asyncio.create_task(
        _process_group(bot, _session_factory, group_id),
        name=f"avail-initial-{group_id}",
    )


# ── shared state (module-level, single event-loop safe) ───────────────────────

_processing_groups: set[int] = set()  # group IDs currently in-flight
_cancel_flags: set[int] = set()        # group IDs with cancel requested

# Per-group cooldown for recently-confirmed-bad IPs: {group_id: {ip: monotonic_time}}
# Prevents cycling between two bad IPs that happen to appear reachable on the first check.
_recently_failed: dict[int, dict[str, float]] = {}
FAILED_IP_COOLDOWN = 1800  # 30 minutes — don't reuse a confirmed-bad IP as replacement

POLL_INTERVAL = 60       # seconds between "which groups are due?" wakeups
LOSS_THRESHOLD = 0.25    # >1 packet out of 4 lost → IP is "lossy", find better


def request_skip(group_id: int) -> None:
    """Called from the bot cancel-button handler to stop a running search."""
    _cancel_flags.add(group_id)


def _mark_ips_failed(group_id: int, ips: set[str]) -> None:
    """Record IPs confirmed bad — exclude them from replacement search for FAILED_IP_COOLDOWN seconds."""
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
    _automation_active.add(group_id)   # signal pool scoring to yield
    try:
        await _do_process_group(bot, session_factory, group_id)
    except Exception:
        logger.exception("Availability monitor: error processing group %d", group_id)
    finally:
        _processing_groups.discard(group_id)
        _automation_active.discard(group_id)


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

    # ── check current IPs: ping + TLS in parallel ────────────────────────────
    # Strip port suffixes: Remnawave may store "1.2.3.4:443", Pingachock wants bare IPs.
    unique_current_ips = list({h.get("address", "").split(":")[0] for h in tagged_hosts if h.get("address")})
    if not unique_current_ips:
        return

    logger.info("Group %d: checking current IPs: %s", group_id, unique_current_ips)

    ping_res_or_exc, tls_res_or_exc = await asyncio.gather(
        distributed_ping_check(pc.api_url, pc.api_key, unique_current_ips),
        tls_check_batch(pc.api_url, pc.api_key, unique_current_ips),
        return_exceptions=True,
    )

    if isinstance(ping_res_or_exc, Exception):
        logger.error("Group %d: ping check failed: %s", group_id, ping_res_or_exc)
        ip_results: dict[str, tuple[bool | None, float | None, float | None]] = {
            ip: (None, None, None) for ip in unique_current_ips
        }
    else:
        ip_results = ping_res_or_exc

    if isinstance(tls_res_or_exc, Exception):
        logger.error("Group %d: TLS check failed: %s", group_id, tls_res_or_exc)
        tls_ip_results: dict[str, tuple[bool | None, float | None]] = {
            ip: (None, None) for ip in unique_current_ips
        }
    else:
        tls_ip_results = tls_res_or_exc

    # Classify each IP: dead / lossy / ok / unknown
    #   dead  — ICMP unreachable OR TLS confirmed failed (False, not None)
    #   lossy — ICMP ok, TLS ok/unknown, but packet loss > threshold
    #   ok    — ICMP ok, TLS ok, loss within threshold
    dead_ips: dict[str, tuple[float | None, float | None]] = {}
    lossy_ips: dict[str, tuple[float | None, float | None]] = {}
    log_parts: dict[str, str] = {}

    for ip, (reachable, loss_pct, avg_rtt_ms) in ip_results.items():
        tls_ok, tls_ms = tls_ip_results.get(ip, (None, None))

        if reachable is None:
            verdict = "UNKNOWN"
        elif not reachable:
            verdict = "DEAD"
            dead_ips[ip] = (loss_pct, avg_rtt_ms)
        elif tls_ok is False:
            verdict = "TLS_FAIL"
            dead_ips[ip] = (loss_pct, avg_rtt_ms)  # treat TLS failure as dead
        elif loss_pct is not None and loss_pct > LOSS_THRESHOLD:
            verdict = "LOSSY"
            lossy_ips[ip] = (loss_pct, avg_rtt_ms)
        else:
            verdict = "OK"

        loss_str = f"loss={loss_pct * 100:.0f}%" if loss_pct is not None else "loss=?"
        rtt_str = f" rtt={avg_rtt_ms:.0f}ms" if avg_rtt_ms is not None else ""
        tls_str = (
            f" tls={tls_ms:.0f}ms" if tls_ms is not None
            else (" tls=FAIL" if tls_ok is False else " tls=?")
        )
        log_parts[ip] = f"{verdict} {loss_str}{rtt_str}{tls_str}"

    logger.info("Group %d: check results: %s", group_id, log_parts)

    if not dead_ips and not lossy_ips:
        logger.info("Group %d: all IPs OK", group_id)
        return

    if dead_ips:
        logger.info("Group %d: dead IPs: %s", group_id, list(dead_ips))
    if lossy_ips:
        logger.info("Group %d: lossy IPs (>%.0f%% loss): %s", group_id, LOSS_THRESHOLD * 100, {
            ip: f"{l*100:.0f}%" for ip, (l, _) in lossy_ips.items() if l is not None
        })

    # All bad IPs go into cooldown — prevents cycling between bad addresses
    _mark_ips_failed(group_id, set(dead_ips) | set(lossy_ips))

    # ip → (loss_pct | None, avg_rtt_ms | None)
    all_bad_ips: dict[str, tuple[float | None, float | None]] = {**dead_ips, **lossy_ips}

    # ── fix bad IPs according to distribution mode ───────────────────────────
    if group.distribution == "same":
        # All hosts share one IP — treat the whole group as a unit.
        bad_ip = next(iter(all_bad_ips))
        is_dead = bad_ip in dead_ips
        current_loss_pct = all_bad_ips[bad_ip][0]

        if group.managed_pool_id:
            # Managed pool mode: get replacement from scored pool
            bad_ips_to_skip = frozenset(
                h.get("address", "").split(":")[0]
                for h in tagged_hosts
                if h.get("address")
            )
            cooldown_ips = _get_cooldown_ips(group.id)
            await _replace_ip_for_hosts_from_pool(
                bot=bot,
                group=group,
                panel=panel,
                pc=pc,
                session_factory=session_factory,
                hosts_to_fix=tagged_hosts,
                display_bad_ip=bad_ip,
                host_label=group.host_tag,
                member_ids=member_ids,
                reason="dead" if is_dead else "lossy",
                current_loss_pct=current_loss_pct,
                exclude_ips=bad_ips_to_skip | cooldown_ips,
            )
        else:
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
                current_loss_pct=current_loss_pct,
            )
    else:
        # "each" distribution — silent background maintenance, no Telegram alerts.
        # Every host in the group must have a unique IP.
        # When replacing a bad IP, we exclude ALL other hosts' current IPs
        # (not just cooldown/bad ones) so the group never has duplicates.

        # Track current IPs of all hosts in the group; updated as replacements are applied.
        group_ips: dict[str, str] = {
            h["uuid"]: h.get("address", "").split(":")[0]
            for h in tagged_hosts
            if h.get("address")
        }
        cooldown_ips = _get_cooldown_ips(group.id)

        for host in tagged_hosts:
            if group.id in _cancel_flags:
                _cancel_flags.discard(group.id)
                break

            host_ip = group_ips.get(host["uuid"], "")
            if host_ip not in all_bad_ips:
                continue

            is_dead = host_ip in dead_ips
            require_low_loss = not is_dead

            # Exclude: bad IP + cooldown + every OTHER host's current IP in this group
            other_ips = frozenset(
                ip for uuid, ip in group_ips.items()
                if uuid != host["uuid"] and ip
            )
            exclude = frozenset({host_ip}) | cooldown_ips | other_ips

            if group.managed_pool_id:
                new_ip, loss_pct, rtt_ms, _speed = await _find_replacement_from_pool(
                    pc.api_url, pc.api_key, session_factory,
                    group.managed_pool_id, group.id, exclude,
                )
            else:
                new_ip, loss_pct, rtt_ms = await _find_replacement(
                    pc.api_url, pc.api_key, pool, group.id,
                    exclude_ips=exclude,
                    require_low_loss=require_low_loss,
                )

            if new_ip:
                try:
                    await update_host_address(panel.url, panel.api_token, host["uuid"], new_ip)
                    group_ips[host["uuid"]] = new_ip  # keep tracking table current
                    parts: list[str] = []
                    if loss_pct is not None:
                        parts.append(f"loss={loss_pct*100:.0f}%")
                    if rtt_ms is not None:
                        parts.append(f"rtt={rtt_ms:.0f}ms")
                    logger.info(
                        "Group %d [each] silent replace: %s → %s (%s)",
                        group.id, host_ip, new_ip,
                        ", ".join(parts) if parts else "no-stats",
                    )
                except RemnaWaveAPIError as e:
                    logger.error(
                        "Group %d: failed to update host %s: %s",
                        group.id, host["uuid"], e,
                    )
            else:
                logger.warning(
                    "Group %d [each]: no replacement found for %s (bad IP: %s)",
                    group.id,
                    host.get("remark") or host["uuid"],
                    host_ip,
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
    reason: str = "dead",                  # "dead" — недоступен; "lossy" — высокие потери
    current_loss_pct: float | None = None, # потери текущего (плохого) IP, None если нет данных
) -> None:
    """Send live alert, search for replacement, apply, edit alert with result."""
    start_t = time.monotonic()

    def _elapsed_str() -> str:
        secs = int(time.monotonic() - start_t)
        m, s = divmod(secs, 60)
        return f"{m}м {s}с" if m else f"{s}с"

    if reason == "lossy":
        ip_status = (
            f"потери {current_loss_pct*100:.0f}%"
            if current_loss_pct is not None
            else "высокие потери"
        )
        alert_base = (
            f"📉 <b>Высокие потери пакетов</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — {ip_status}\n"
            f"Ищем замену из: <b>{set_names}</b>"
        )
    else:
        alert_base = (
            f"⚠️ <b>Недоступный IP</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — недоступен\n"
            f"Ищем замену из: <b>{set_names}</b>"
        )
    skip_kb = _skip_kb(group.id)

    # Send initial alert to all org members in parallel
    async def _send_initial(chat_id: int) -> tuple[int, int] | None:
        try:
            msg = await bot.send_message(
                chat_id,
                alert_base + "\n\nПрошло: 0с",
                parse_mode="HTML",
                reply_markup=skip_kb,
            )
            return (chat_id, msg.message_id)
        except Exception:
            return None

    send_results = await asyncio.gather(*[_send_initial(cid) for cid in member_ids])
    alert_msgs: list[tuple[int, int]] = [r for r in send_results if r is not None]

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
    new_loss_pct: float | None = None
    new_rtt_ms: float | None = None
    cancelled = False
    try:
        new_ip, new_loss_pct, new_rtt_ms = await _find_replacement(
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

        # Quality line: latency and loss of the new IP
        quality_parts: list[str] = []
        if new_rtt_ms is not None:
            quality_parts.append(f"задержка {new_rtt_ms:.0f}мс")
        if new_loss_pct is not None:
            quality_parts.append(f"потери {new_loss_pct*100:.0f}%")
        quality_line = ", ".join(quality_parts)

        verb = "улучшен" if reason == "lossy" else "заменён"
        result_text = (
            f"✅ <b>Адрес {verb}</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"<code>{display_bad_ip}</code> → <code>{new_ip}</code>"
        )
        if quality_line:
            result_text += f"\n{quality_line}"
        result_text += f"\n\nВремя поиска: {elapsed}"

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

    # Edit all alert messages with final result in parallel
    async def _edit_final(chat_id: int, msg_id: int) -> None:
        try:
            await bot.edit_message_text(
                result_text,
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="HTML",
            )
        except Exception:
            pass

    await asyncio.gather(*[_edit_final(cid, mid) for cid, mid in alert_msgs])


async def _find_replacement_from_pool(
    api_url: str,
    api_key: str,
    session_factory: async_sessionmaker,
    pool_id: int,
    group_id: int,
    exclude_ips: frozenset[str],
) -> tuple[str, float | None, float | None, float | None] | tuple[None, None, None, None]:
    """Get best replacement from a scored managed pool.

    Returns (ip, loss_pct, rtt_ms, speed_mbps) or (None, None, None, None).
    Runs a final ping check to verify the IP is still alive before returning.
    """
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        candidates: list[ManagedIp] = await pool_svc.get_approved_ips(pool_id)

    filtered = [c for c in candidates if c.ip not in exclude_ips]
    if not filtered:
        logger.warning("Pool %d: no approved IPs available (excluding %d IPs)", pool_id, len(exclude_ips))
        return None, None, None, None

    # Final liveness check in batches before committing to a replacement
    for i in range(0, len(filtered), CHECK_BATCH):
        if group_id in _cancel_flags:
            return None, None, None, None
        batch = filtered[i: i + CHECK_BATCH]
        batch_ips = [c.ip for c in batch]
        try:
            ping_results = await distributed_ping_check(api_url, api_key, batch_ips)
        except PingachockAPIError as e:
            logger.warning("Pool %d: final ping batch failed: %s", pool_id, e)
            continue
        for candidate in batch:
            reachable, _, _ = ping_results.get(candidate.ip, (None, None, None))
            if reachable is True:  # confirmed; None (timeout) = skip, need verified IPs
                logger.info(
                    "Pool %d: replacement %s selected (score=%.0f, speed=%s Mbps)",
                    pool_id, candidate.ip, candidate.score,
                    f"{candidate.vless_speed_mbps:.0f}" if candidate.vless_speed_mbps else "?",
                )
                return candidate.ip, candidate.ping_loss_pct, candidate.ping_rtt_ms, candidate.vless_speed_mbps

    logger.warning("Pool %d: all approved candidates failed final ping check", pool_id)
    return None, None, None, None


async def _replace_ip_for_hosts_from_pool(
    bot: Bot,
    group: AutomationGroup,
    panel,
    pc,
    session_factory: async_sessionmaker,
    hosts_to_fix: list[dict],
    display_bad_ip: str,
    host_label: str,
    member_ids: list[int],
    reason: str = "dead",
    current_loss_pct: float | None = None,
    exclude_ips: frozenset[str] = frozenset(),
) -> None:
    """Managed-pool variant of _replace_ip_for_hosts.

    Sends alert, queries managed pool for best replacement, applies, edits alert.
    """
    start_t = time.monotonic()

    def _elapsed_str() -> str:
        secs = int(time.monotonic() - start_t)
        m, s = divmod(secs, 60)
        return f"{m}м {s}с" if m else f"{s}с"

    if reason == "lossy":
        ip_status = (
            f"потери {current_loss_pct*100:.0f}%"
            if current_loss_pct is not None else "высокие потери"
        )
        alert_base = (
            f"📉 <b>Высокие потери пакетов</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — {ip_status}\n"
            f"Ищем замену в управляемом пуле..."
        )
    else:
        alert_base = (
            f"⚠️ <b>Недоступный IP</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — недоступен\n"
            f"Ищем замену в управляемом пуле..."
        )

    skip_kb = _skip_kb(group.id)

    async def _send_initial(chat_id: int) -> tuple[int, int] | None:
        try:
            msg = await bot.send_message(
                chat_id,
                alert_base + "\n\nПрошло: 0с",
                parse_mode="HTML",
                reply_markup=skip_kb,
            )
            return (chat_id, msg.message_id)
        except Exception:
            return None

    send_results = await asyncio.gather(*[_send_initial(cid) for cid in member_ids])
    alert_msgs: list[tuple[int, int]] = [r for r in send_results if r is not None]

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

    new_ip: str | None = None
    new_loss_pct: float | None = None
    new_rtt_ms: float | None = None
    new_speed_mbps: float | None = None
    cancelled = False
    try:
        new_ip, new_loss_pct, new_rtt_ms, new_speed_mbps = await _find_replacement_from_pool(
            pc.api_url, pc.api_key, session_factory,
            group.managed_pool_id, group.id, exclude_ips,
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

    if new_ip and not cancelled:
        for h in hosts_to_fix:
            try:
                await update_host_address(panel.url, panel.api_token, h["uuid"], new_ip)
            except RemnaWaveAPIError as e:
                logger.error("Group %d: failed to update host %s: %s", group.id, h["uuid"], e)

        quality_parts: list[str] = []
        if new_rtt_ms is not None:
            quality_parts.append(f"задержка {new_rtt_ms:.0f}мс")
        if new_loss_pct is not None:
            quality_parts.append(f"потери {new_loss_pct*100:.0f}%")
        if new_speed_mbps is not None:
            quality_parts.append(f"скорость {new_speed_mbps:.0f} Мбит/с")

        verb = "улучшен" if reason == "lossy" else "заменён"
        result_text = (
            f"✅ <b>Адрес {verb}</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"<code>{display_bad_ip}</code> → <code>{new_ip}</code>"
        )
        if quality_parts:
            result_text += "\n" + ", ".join(quality_parts)
        result_text += f"\n\nВремя поиска: {elapsed}"

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
            f"IP: <code>{display_bad_ip}</code> — в управляемом пуле нет подходящих адресов\n\n"
            f"Время поиска: {elapsed}"
        )

    async def _edit_final(chat_id: int, msg_id: int) -> None:
        try:
            await bot.edit_message_text(
                result_text,
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="HTML",
            )
        except Exception:
            pass

    await asyncio.gather(*[_edit_final(cid, mid) for cid, mid in alert_msgs])


async def _find_replacement(
    api_url: str,
    api_key: str,
    pool: list[str],
    group_id: int,
    exclude_ips: frozenset[str] = frozenset(),
    require_low_loss: bool = False,
) -> tuple[str, float | None, float | None] | tuple[None, None, None]:
    """Scan pool in batches, verify each candidate via a single ping check.

    require_low_loss=False (dead IP): accept any reachable IP.
    require_low_loss=True  (lossy IP): only accept IPs with loss ≤ LOSS_THRESHOLD
                                       or with no loss data (None — unknown).

    exclude_ips: always skipped (current bad IPs + cooldown cache).
    Returns (ip, loss_pct, avg_rtt_ms) or (None, None, None) if not found / cancelled.
    """
    offset = 0
    while offset < len(pool):
        if group_id in _cancel_flags:
            return None, None, None
        batch = [ip for ip in pool[offset: offset + CHECK_BATCH] if ip not in exclude_ips]
        offset += CHECK_BATCH
        if not batch:
            continue
        try:
            results = await distributed_ping_check(api_url, api_key, batch)
        except Exception as e:
            logger.warning("Group %d: ping error during replacement search: %s", group_id, e)
            continue
        for ip in batch:
            reachable, loss_pct, avg_rtt_ms = results.get(ip, (None, None, None))
            if reachable is not True:  # False = dead, None = timeout; only use confirmed IPs
                continue
            # If loss data is available and exceeds threshold — skip (when require_low_loss).
            # If loss data is absent (None) — accept: no evidence of lossy behaviour.
            if require_low_loss and loss_pct is not None and loss_pct > LOSS_THRESHOLD:
                logger.debug(
                    "Group %d: skip %s — reachable but loss=%.0f%% > threshold",
                    group_id, ip, loss_pct * 100,
                )
                continue
            parts = []
            if loss_pct is not None:
                parts.append(f"loss={loss_pct*100:.0f}%")
            if avg_rtt_ms is not None:
                parts.append(f"rtt={avg_rtt_ms:.0f}ms")
            logger.info(
                "Group %d: replacement %s found (%s)",
                group_id, ip, ", ".join(parts) if parts else "no-stats",
            )
            return ip, loss_pct, avg_rtt_ms
    return None, None, None


# ── inline keyboard for skip button ──────────────────────────────────────────

def _skip_kb(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏭ Пропустить эту проверку",
            callback_data=f"avail:skip:{group_id}",
        )],
    ])
