"""Background service: Lightsail automatic IP search.

Search flow for one region config:
  1. Create a Lightsail nano Debian instance (if not already running).
  2. Open all ports (TCP+UDP full range + ICMP).
  3. Loop:
       a. If total_static_ips < 5: allocate a new static IP.
       b. If total_static_ips == 5 and need more: release one non-working IP,
          allocate new.
       c. Attach the new static IP to the instance (detach previous if needed).
       d. Wait 35s for propagation.
       e. Check via Pingachock (using config's node_ids).
       f. Record result. If working: notify + add to found list.
       g. If working_count >= target_count → finish.
  4. Cleanup: delete non-working static IPs from Lightsail; delete instance.
  5. Mark config status: 'monitoring' if target reached, 'idle' otherwise.
  6. Notify all org users of the outcome.

Periodic recheck (monitoring status):
  - Creates instance, attaches each working IP, checks via Pingachock.
  - If all OK → notification 8 (all good).
  - If any failed → find replacement inline → notification 10.
  - Deletes instance, updates last_recheck_at.

Loss thresholds:
  - Elapsed < 1h: require loss_pct == 0 (or None = no data → accept).
  - Elapsed ≥ 1h: accept any reachable IP (loss_pct < 0.75).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models.organization_member import OrganizationMember
from services.aws_service import AWSService
from services.ip_check_service import distributed_ping_check
from services.lightsail_api_service import (
    allocate_static_ip,
    attach_static_ip,
    create_instance,
    delete_instance,
    detach_static_ip,
    get_region_static_ips,
    instance_exists,
    open_all_ports,
    release_static_ip,
    wait_for_instance_running,
)
from services.lightsail_search_service import LightsailSearchService
from services.pingachock_service import PingachockService, build_node_selector

logger = logging.getLogger(__name__)

MAX_STATIC_IPS = 5
ATTACH_WAIT_SECONDS = 15
WAKEUP_INTERVAL = 60
LOSS_THRESHOLD_NORMAL = 0.0
LOSS_THRESHOLD_RELAXED = 0.75
RELAX_AFTER_SECONDS = 3600

# ── in-memory task registry ───────────────────────────────────────────────────

_session_factory: "async_sessionmaker | None" = None
_bot: "Bot | None" = None
_active_tasks: dict[int, asyncio.Task] = {}   # config_id → Task


def init(session_factory: async_sessionmaker, bot: Bot) -> None:
    global _session_factory, _bot
    _session_factory = session_factory
    _bot = bot


# ── public API ────────────────────────────────────────────────────────────────

def is_running(config_id: int) -> bool:
    task = _active_tasks.get(config_id)
    return task is not None and not task.done()


async def start_search(config_id: int) -> None:
    if is_running(config_id):
        return
    task = asyncio.create_task(
        _search_safe(config_id),
        name=f"lightsail-search-{config_id}",
    )
    _active_tasks[config_id] = task


async def stop_search(config_id: int) -> None:
    """Cancel running task and delete the Lightsail instance."""
    task = _active_tasks.pop(config_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if _session_factory:
        async with _session_factory() as session:
            svc = LightsailSearchService(session)
            cfg = await svc.get_config_by_id(config_id)
            if cfg and cfg.instance_name:
                aws = await AWSService(session).get_account_by_id(
                    cfg.aws_account_id, cfg.org_id
                )
                if aws:
                    try:
                        await delete_instance(
                            cfg.region, aws.access_key_id, aws.secret_access_key,
                            cfg.instance_name,
                        )
                    except Exception as e:
                        logger.warning("stop_search: could not delete instance: %s", e)
            if cfg:
                await svc.mark_paused(config_id)


# ── notifications ─────────────────────────────────────────────────────────────

async def notify_org(org_id: int, text: str) -> None:
    """Send a message to every member of org_id."""
    if _bot is None or _session_factory is None:
        return
    async with _session_factory() as session:
        res = await session.execute(
            select(OrganizationMember.user_id).where(
                OrganizationMember.org_id == org_id
            )
        )
        user_ids = list(res.scalars().all())
    for uid in user_ids:
        try:
            await _bot.send_message(uid, text, parse_mode="HTML")
        except (TelegramForbiddenError, TelegramBadRequest):
            pass   # user blocked the bot or chat not found
        except Exception as e:
            logger.debug("notify_org: uid=%d error=%s", uid, e)


def _fmt_region(cfg) -> str:
    return cfg.region_display_name or cfg.region


def _elapsed_str(started_at: datetime | None) -> str:
    if started_at is None:
        return "—"
    secs = int((datetime.utcnow() - started_at).total_seconds())
    if secs < 0:
        secs = 0
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}ч {m}м {s}с"
    return f"{m}м {s}с" if m else f"{s}с"


# ── scheduler ─────────────────────────────────────────────────────────────────

async def run_lightsail_searcher(session_factory: async_sessionmaker, bot: Bot) -> None:
    init(session_factory, bot)
    logger.info("Lightsail searcher started")
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("Lightsail searcher: unhandled error in tick")
        await asyncio.sleep(WAKEUP_INTERVAL)


async def _tick() -> None:
    if _session_factory is None:
        return

    async with _session_factory() as session:
        svc = LightsailSearchService(session)
        searching = await svc.get_all_searching()
        monitoring = await svc.get_all_monitoring()

    # Resume searching configs that lost their task (e.g. after restart)
    for cfg in searching:
        if not is_running(cfg.id):
            logger.info("Lightsail: resuming search config %d (%s)", cfg.id, cfg.region)
            await start_search(cfg.id)

    # Trigger recheck for monitoring configs whose interval has elapsed
    now = datetime.utcnow()
    for cfg in monitoring:
        if is_running(cfg.id):
            continue
        last = cfg.last_recheck_at
        elapsed_min = (now - last).total_seconds() / 60 if last else float("inf")
        if elapsed_min >= cfg.recheck_minutes:
            logger.info("Lightsail: triggering recheck for config %d (%s)", cfg.id, cfg.region)
            task = asyncio.create_task(
                _recheck_safe(cfg.id),
                name=f"lightsail-recheck-{cfg.id}",
            )
            _active_tasks[cfg.id] = task


# ── core search ───────────────────────────────────────────────────────────────

async def _search_safe(config_id: int) -> None:
    try:
        await _search(config_id)
    except asyncio.CancelledError:
        logger.info("Lightsail search %d: cancelled", config_id)
    except Exception:
        logger.exception("Lightsail search %d: unexpected error", config_id)
        if _session_factory:
            async with _session_factory() as session:
                await LightsailSearchService(session).set_status(config_id, "idle")
    finally:
        _active_tasks.pop(config_id, None)


async def _search(config_id: int) -> None:  # noqa: C901
    if _session_factory is None:
        return

    # ── load config and credentials ──────────────────────────────────────────
    async with _session_factory() as session:
        svc = LightsailSearchService(session)
        cfg = await svc.get_config_by_id(config_id)
        if not cfg:
            return
        aws = await AWSService(session).get_account_by_id(cfg.aws_account_id, cfg.org_id)
        if not aws:
            logger.error("Search %d: AWS account not found", config_id)
            await svc.set_status(config_id, "idle")
            return
        pc = await PingachockService(session).get_settings(cfg.org_id)
        if not pc:
            logger.error("Search %d: Pingachock not configured", config_id)
            await svc.set_status(config_id, "idle")
            return

    org_id = cfg.org_id
    region = cfg.region
    region_label = _fmt_region(cfg)
    ak, sk = aws.access_key_id, aws.secret_access_key
    api_url, api_key = pc.api_url, pc.api_key
    node_selector = build_node_selector(cfg)
    search_start = time.monotonic()

    # ── create / verify instance ─────────────────────────────────────────────
    instance_name = f"fcc-searcher-{config_id}"
    if not await instance_exists(region, ak, sk, instance_name):
        try:
            await create_instance(region, ak, sk, instance_name)
        except Exception as e:
            logger.error("Search %d: failed to create instance: %s", config_id, e)
            async with _session_factory() as session:
                await LightsailSearchService(session).set_status(config_id, "idle")
            return

    running = await wait_for_instance_running(region, ak, sk, instance_name)
    if not running:
        logger.error("Search %d: instance never reached running state", config_id)
        async with _session_factory() as session:
            await LightsailSearchService(session).set_status(config_id, "idle")
        return

    try:
        await open_all_ports(region, ak, sk, instance_name)
    except Exception as e:
        logger.warning("Search %d: could not open ports: %s", config_id, e)

    async with _session_factory() as session:
        cfg = await LightsailSearchService(session).get_config_by_id(config_id)
        await LightsailSearchService(session).mark_search_started(config_id, instance_name)

    # ── notify: search started ───────────────────────────────────────────────
    await notify_org(
        org_id,
        f"🔴 <b>Amazon Lightsail [{region_label}]</b> — поиск запущен\n"
        f"Цель: {cfg.target_count} рабочих IP",
    )

    # ── reconcile DB with actual AWS static IPs ──────────────────────────────
    # (handles restart after crash — AWS IPs may exist that aren't in DB)
    await _reconcile_static_ips(config_id, region, ak, sk)

    logger.info("Search %d [%s]: instance ready, starting IP search", config_id, region)

    # ── main search loop ─────────────────────────────────────────────────────
    while True:
        async with _session_factory() as session:
            svc = LightsailSearchService(session)
            cfg = await svc.get_config_by_id(config_id)
            if not cfg or cfg.status != "searching":
                break
            target = cfg.target_count
            node_selector = build_node_selector(cfg)
            all_ips = await svc.get_all_ips(config_id)

        working = [ip for ip in all_ips if ip.is_working is True]
        # Rotation candidates: non-working OR untested (is_working=None, e.g. after restart)
        rotation_candidates = [ip for ip in all_ips if ip.is_working is not True]
        total = len(all_ips)

        if len(working) >= target:
            break

        # ── determine which IP to test next ───────────────────────────────
        new_ip_name: str
        new_ip_addr: str

        if total < MAX_STATIC_IPS:
            new_name = f"fcc-{config_id}-{uuid.uuid4().hex[:6]}"
            ip_info = await _allocate_with_quota_recovery(
                config_id, region, ak, sk, new_name
            )
            if ip_info is None:
                await asyncio.sleep(30)
                continue
            new_ip_name = ip_info["name"]
            new_ip_addr = ip_info["ipAddress"]
            async with _session_factory() as session:
                await LightsailSearchService(session).add_static_ip(
                    config_id, new_ip_name, new_ip_addr, is_attached=False
                )
        else:
            if not rotation_candidates:
                # All 5 slots are working — should not happen, but safety break
                break
            victim = rotation_candidates[0]
            try:
                await detach_static_ip(region, ak, sk, victim.static_ip_name)
                await release_static_ip(region, ak, sk, victim.static_ip_name)
            except Exception as e:
                logger.warning("Search %d: could not release %s: %s",
                               config_id, victim.static_ip_name, e)
            async with _session_factory() as session:
                await LightsailSearchService(session).delete_static_ip(victim.static_ip_name)

            new_name = f"fcc-{config_id}-{uuid.uuid4().hex[:6]}"
            ip_info = await _allocate_with_quota_recovery(
                config_id, region, ak, sk, new_name
            )
            if ip_info is None:
                await asyncio.sleep(30)
                continue
            new_ip_name = ip_info["name"]
            new_ip_addr = ip_info["ipAddress"]
            async with _session_factory() as session:
                await LightsailSearchService(session).add_static_ip(
                    config_id, new_ip_name, new_ip_addr, is_attached=False
                )

        # ── detach currently attached IP ───────────────────────────────────
        currently_attached = [ip for ip in all_ips if ip.is_attached]
        for att in currently_attached:
            try:
                await detach_static_ip(region, ak, sk, att.static_ip_name)
            except Exception:
                pass
            async with _session_factory() as session:
                await LightsailSearchService(session).set_ip_attached(att.static_ip_name, False)

        # ── attach new IP ──────────────────────────────────────────────────
        try:
            await attach_static_ip(region, ak, sk, new_ip_name, instance_name)
        except Exception as e:
            logger.error("Search %d: failed to attach %s: %s", config_id, new_ip_name, e)
            async with _session_factory() as session:
                await LightsailSearchService(session).set_ip_result(new_ip_name, False)
            continue

        async with _session_factory() as session:
            await LightsailSearchService(session).set_ip_attached(new_ip_name, True)

        logger.info("Search %d: waiting %ds for %s to propagate",
                    config_id, ATTACH_WAIT_SECONDS, new_ip_addr)
        await asyncio.sleep(ATTACH_WAIT_SECONDS)

        # ── Pingachock check ───────────────────────────────────────────────
        elapsed = time.monotonic() - search_start
        try:
            results = await distributed_ping_check(
                api_url, api_key, [new_ip_addr], node_selector=node_selector,
            )
            reachable, loss_pct, rtt_ms = results.get(new_ip_addr, (None, None, None))
        except Exception as e:
            logger.warning("Search %d: Pingachock error for %s: %s", config_id, new_ip_addr, e)
            reachable, loss_pct, rtt_ms = None, None, None

        is_working = _is_ip_working(reachable, loss_pct, elapsed)

        logger.info("Search %d: %s → %s (loss=%s)",
                    config_id, new_ip_addr,
                    "working" if is_working else "not working",
                    f"{loss_pct*100:.0f}%" if loss_pct is not None else "?")

        async with _session_factory() as session:
            svc = LightsailSearchService(session)
            await svc.set_ip_result(new_ip_name, is_working=is_working, is_attached=True)
            await svc.increment_checked(config_id)
            cfg_now = await svc.get_config_by_id(config_id)
            working_now = await svc.get_working_ips(config_id)

        # ── notify: working IP found ───────────────────────────────────────
        if is_working and cfg_now:
            found = len(working_now)
            target_now = cfg_now.target_count
            loss_str = f"{loss_pct*100:.0f}%" if loss_pct is not None else "0%"
            await notify_org(
                org_id,
                f"✅ <b>Amazon Lightsail [{region_label}]</b> — найден рабочий IP\n"
                f"<code>{new_ip_addr}</code> · потери: {loss_str}\n"
                f"Найдено: {found} из {target_now} · Проверено: {cfg_now.total_checked}",
            )

    # ── cleanup ───────────────────────────────────────────────────────────────
    async with _session_factory() as session:
        svc = LightsailSearchService(session)
        all_ips = await svc.get_all_ips(config_id)
        non_working_names = [ip.static_ip_name for ip in all_ips if not ip.is_working]

    for name in non_working_names:
        try:
            await detach_static_ip(region, ak, sk, name)
            await release_static_ip(region, ak, sk, name)
        except Exception as e:
            logger.warning("Search %d cleanup: could not release %s: %s", config_id, name, e)
        async with _session_factory() as session:
            await LightsailSearchService(session).delete_static_ip(name)

    async with _session_factory() as session:
        all_ips = await LightsailSearchService(session).get_all_ips(config_id)
    for ip in all_ips:
        if ip.is_attached:
            try:
                await detach_static_ip(region, ak, sk, ip.static_ip_name)
            except Exception:
                pass
            async with _session_factory() as session:
                await LightsailSearchService(session).set_ip_attached(ip.static_ip_name, False)

    try:
        await delete_instance(region, ak, sk, instance_name)
    except Exception as e:
        logger.warning("Search %d cleanup: could not delete instance: %s", config_id, e)

    async with _session_factory() as session:
        svc = LightsailSearchService(session)
        await svc.mark_search_stopped(config_id)
        cfg_final = await svc.get_config_by_id(config_id)
        working_final = await svc.get_working_ips(config_id)

    # ── notify: search finished ────────────────────────────────────────────
    if cfg_final:
        found = len(working_final)
        target_final = cfg_final.target_count
        elapsed_str = _elapsed_str(cfg_final.search_started_at)
        checked = cfg_final.total_checked

        if found >= target_final:
            ips_str = "\n".join(f"<code>{ip.ip_address}</code>" for ip in working_final)
            await notify_org(
                org_id,
                f"🎯 <b>Amazon Lightsail [{region_label}]</b> — поиск завершён!\n"
                f"Найдено {found} рабочих адресов:\n"
                f"{ips_str}\n"
                f"Проверено: {checked} · Затрачено: {elapsed_str}",
            )
        else:
            await notify_org(
                org_id,
                f"⏹ <b>Amazon Lightsail [{region_label}]</b> — поиск остановлен\n"
                f"Найдено: {found} из {target_final} · Проверено: {checked}",
            )

    logger.info("Search %d [%s]: finished", config_id, region)


# ── periodic recheck ──────────────────────────────────────────────────────────

async def _recheck_safe(config_id: int) -> None:
    try:
        await _recheck(config_id)
    except asyncio.CancelledError:
        logger.info("Lightsail recheck %d: cancelled", config_id)
    except Exception:
        logger.exception("Lightsail recheck %d: unexpected error", config_id)
    finally:
        _active_tasks.pop(config_id, None)


async def _recheck(config_id: int) -> None:  # noqa: C901
    if _session_factory is None:
        return

    async with _session_factory() as session:
        svc = LightsailSearchService(session)
        cfg = await svc.get_config_by_id(config_id)
        if not cfg or cfg.status != "monitoring":
            return
        aws = await AWSService(session).get_account_by_id(cfg.aws_account_id, cfg.org_id)
        if not aws:
            return
        pc = await PingachockService(session).get_settings(cfg.org_id)
        if not pc:
            return
        working_ips = await svc.get_working_ips(config_id)

    if not working_ips:
        async with _session_factory() as session:
            await LightsailSearchService(session).set_status(config_id, "idle")
        return

    org_id = cfg.org_id
    region = cfg.region
    region_label = _fmt_region(cfg)
    ak, sk = aws.access_key_id, aws.secret_access_key
    api_url, api_key = pc.api_url, pc.api_key
    node_selector = build_node_selector(cfg)
    instance_name = f"fcc-searcher-{config_id}"

    logger.info("Recheck %d [%s]: starting periodic recheck of %d IPs",
                config_id, region, len(working_ips))

    # ── create instance ────────────────────────────────────────────────────
    if not await instance_exists(region, ak, sk, instance_name):
        try:
            await create_instance(region, ak, sk, instance_name)
        except Exception as e:
            logger.error("Recheck %d: failed to create instance: %s", config_id, e)
            return

    running = await wait_for_instance_running(region, ak, sk, instance_name)
    if not running:
        logger.error("Recheck %d: instance never started", config_id)
        try:
            await delete_instance(region, ak, sk, instance_name)
        except Exception:
            pass
        return

    try:
        await open_all_ports(region, ak, sk, instance_name)
    except Exception:
        pass

    # ── check each working IP ──────────────────────────────────────────────
    # results: ip_address → (reachable, loss_pct)
    check_results: dict[str, tuple[bool, float | None]] = {}
    currently_attached: str | None = None

    for ip_row in working_ips:
        ip_addr = ip_row.ip_address
        ip_name = ip_row.static_ip_name

        # Detach previous
        if currently_attached and currently_attached != ip_name:
            try:
                await detach_static_ip(region, ak, sk, currently_attached)
            except Exception:
                pass

        # Attach this IP
        try:
            await attach_static_ip(region, ak, sk, ip_name, instance_name)
            currently_attached = ip_name
        except Exception as e:
            logger.warning("Recheck %d: could not attach %s: %s", config_id, ip_name, e)
            check_results[ip_addr] = (False, None)
            continue

        await asyncio.sleep(ATTACH_WAIT_SECONDS)

        try:
            results = await distributed_ping_check(
                api_url, api_key, [ip_addr], node_selector=node_selector,
            )
            reachable, loss_pct, _ = results.get(ip_addr, (None, None, None))
        except Exception as e:
            logger.warning("Recheck %d: Pingachock error for %s: %s", config_id, ip_addr, e)
            reachable, loss_pct = None, None

        # For recheck use relaxed threshold (already established IPs)
        ok = reachable is True and (loss_pct is None or loss_pct < LOSS_THRESHOLD_RELAXED)
        check_results[ip_addr] = (ok, loss_pct)
        logger.info("Recheck %d: %s → %s (loss=%s)",
                    config_id, ip_addr, "ok" if ok else "FAILED",
                    f"{loss_pct*100:.0f}%" if loss_pct is not None else "?")

    # Detach last
    if currently_attached:
        try:
            await detach_static_ip(region, ak, sk, currently_attached)
        except Exception:
            pass

    failed_ips = [ip for ip in working_ips if not check_results.get(ip.ip_address, (False,))[0]]
    good_ips = [ip for ip in working_ips if check_results.get(ip.ip_address, (False,))[0]]

    # ── find replacements for failed IPs ───────────────────────────────────
    replacements: dict[str, str] = {}   # old_ip_addr → new_ip_addr

    for failed in failed_ips:
        logger.info("Recheck %d: %s failed, searching replacement", config_id, failed.ip_address)
        replacement_addr = await _find_one_replacement(
            config_id, region, ak, sk, api_url, api_key, node_selector, instance_name,
        )
        if replacement_addr:
            replacements[failed.ip_address] = replacement_addr
            # Release the failed IP from AWS and DB
            try:
                await release_static_ip(region, ak, sk, failed.static_ip_name)
            except Exception as e:
                logger.warning("Recheck %d: could not release %s: %s",
                               config_id, failed.static_ip_name, e)
            async with _session_factory() as session:
                await LightsailSearchService(session).delete_static_ip(failed.static_ip_name)
        else:
            logger.warning("Recheck %d: could not find replacement for %s",
                           config_id, failed.ip_address)

    # ── delete instance ────────────────────────────────────────────────────
    try:
        await delete_instance(region, ak, sk, instance_name)
    except Exception as e:
        logger.warning("Recheck %d: could not delete instance: %s", config_id, e)

    # ── update last_recheck_at ─────────────────────────────────────────────
    async with _session_factory() as session:
        await LightsailSearchService(session).mark_recheck(config_id)
        working_now = await LightsailSearchService(session).get_working_ips(config_id)
        cfg_now = await LightsailSearchService(session).get_config_by_id(config_id)

    recheck_interval = cfg_now.recheck_minutes if cfg_now else cfg.recheck_minutes
    total_count = len(working_now)

    # ── notify ────────────────────────────────────────────────────────────
    if not failed_ips:
        # All IPs OK — notification 8
        lines = [f"💚 <b>Периодическая проверка Amazon Lightsail [{region_label}]</b>"]
        ip_stats = []
        for ip_row in good_ips:
            _, loss_pct = check_results.get(ip_row.ip_address, (True, None))
            loss_str = f"{loss_pct*100:.0f}%" if loss_pct is not None else "0%"
            ip_stats.append(f"<code>{ip_row.ip_address}</code> — {loss_str}")
        lines.append(" · ".join(ip_stats))
        lines.append(f"Следующая проверка через {recheck_interval} мин")
        await notify_org(org_id, "\n".join(lines))
    else:
        # Some failed — notification 10 (one message per replaced IP)
        for old_addr, new_addr in replacements.items():
            # Recalculate final stats for the notification
            async with _session_factory() as session:
                final_ips = await LightsailSearchService(session).get_working_ips(config_id)
            final_ok = len(final_ips)
            await notify_org(
                org_id,
                f"✅ <b>Периодическая проверка Amazon Lightsail [{region_label}]</b>\n"
                f"<code>{old_addr}</code> — не отвечает, заменён на <code>{new_addr}</code>\n"
                f"Проверено: {final_ok}/{final_ok} · Потери: 0%\n"
                f"Следующая проверка через {recheck_interval} мин",
            )

        # Failed IPs with no replacement found
        unresolved = [f for f in failed_ips if f.ip_address not in replacements]
        if unresolved:
            for ip_row in unresolved:
                await notify_org(
                    org_id,
                    f"⚠️ <b>Amazon Lightsail [{region_label}]</b>\n"
                    f"<code>{ip_row.ip_address}</code> — не отвечает, замена не найдена\n"
                    f"Рабочих адресов: {total_count} из {cfg.target_count}",
                )

    logger.info("Recheck %d [%s]: done. good=%d failed=%d replaced=%d",
                config_id, region, len(good_ips), len(failed_ips), len(replacements))


async def _find_one_replacement(
    config_id: int,
    region: str,
    ak: str,
    sk: str,
    api_url: str,
    api_key: str,
    node_selector: dict,
    instance_name: str,
    max_attempts: int = 10,
) -> str | None:
    """Try up to max_attempts static IPs to find one working replacement.

    Returns the new IP address if found, None otherwise.
    """
    for attempt in range(max_attempts):
        async with _session_factory() as session:
            all_ips = await LightsailSearchService(session).get_all_ips(config_id)
        non_working = [ip for ip in all_ips if ip.is_working is False]
        total = len(all_ips)

        if total < MAX_STATIC_IPS:
            new_name = f"fcc-{config_id}-{uuid.uuid4().hex[:6]}"
            try:
                ip_info = await allocate_static_ip(region, ak, sk, new_name)
            except Exception as e:
                logger.error("_find_one_replacement: allocate failed: %s", e)
                await asyncio.sleep(15)
                continue
        else:
            # Release a non-working IP to make room
            if not non_working:
                return None
            victim = non_working[0]
            try:
                await detach_static_ip(region, ak, sk, victim.static_ip_name)
                await release_static_ip(region, ak, sk, victim.static_ip_name)
            except Exception as e:
                logger.warning("_find_one_replacement: could not release %s: %s",
                               victim.static_ip_name, e)
            async with _session_factory() as session:
                await LightsailSearchService(session).delete_static_ip(victim.static_ip_name)

            new_name = f"fcc-{config_id}-{uuid.uuid4().hex[:6]}"
            try:
                ip_info = await allocate_static_ip(region, ak, sk, new_name)
            except Exception as e:
                logger.error("_find_one_replacement: allocate failed: %s", e)
                await asyncio.sleep(15)
                continue

        new_ip_name = ip_info["name"]
        new_ip_addr = ip_info["ipAddress"]

        async with _session_factory() as session:
            await LightsailSearchService(session).add_static_ip(
                config_id, new_ip_name, new_ip_addr, is_attached=False
            )

        # Detach whatever is attached
        async with _session_factory() as session:
            all_ips2 = await LightsailSearchService(session).get_all_ips(config_id)
        for ip in all_ips2:
            if ip.is_attached:
                try:
                    await detach_static_ip(region, ak, sk, ip.static_ip_name)
                except Exception:
                    pass
                async with _session_factory() as session:
                    await LightsailSearchService(session).set_ip_attached(ip.static_ip_name, False)

        # Attach new IP
        try:
            await attach_static_ip(region, ak, sk, new_ip_name, instance_name)
        except Exception as e:
            logger.error("_find_one_replacement: attach failed: %s", e)
            async with _session_factory() as session:
                await LightsailSearchService(session).set_ip_result(new_ip_name, False)
            continue

        async with _session_factory() as session:
            await LightsailSearchService(session).set_ip_attached(new_ip_name, True)

        await asyncio.sleep(ATTACH_WAIT_SECONDS)

        try:
            results = await distributed_ping_check(
                api_url, api_key, [new_ip_addr], node_selector=node_selector,
            )
            reachable, loss_pct, _ = results.get(new_ip_addr, (None, None, None))
        except Exception:
            reachable, loss_pct = None, None

        ok = reachable is True and (loss_pct is None or loss_pct < LOSS_THRESHOLD_RELAXED)

        async with _session_factory() as session:
            await LightsailSearchService(session).set_ip_result(
                new_ip_name, is_working=ok, is_attached=True
            )

        if ok:
            logger.info("_find_one_replacement: found %s after %d attempts",
                        new_ip_addr, attempt + 1)
            return new_ip_addr

    return None


# ── helpers ───────────────────────────────────────────────────────────────────

async def _reconcile_static_ips(
    config_id: int, region: str, ak: str, sk: str
) -> None:
    """Sync lightsail_static_ips DB table with what's actually on AWS.

    Adds IPs that exist on AWS but are missing from DB (e.g. after crash/restart).
    Removes IPs from DB that no longer exist on AWS (e.g. manually released).
    """
    prefix = f"fcc-{config_id}-"
    try:
        aws_ips = await get_region_static_ips(region, ak, sk, name_prefix=prefix)
    except Exception as e:
        logger.warning("Reconcile %d: failed to list AWS static IPs: %s", config_id, e)
        return

    aws_by_name = {ip["name"]: ip for ip in aws_ips}

    async with _session_factory() as session:
        svc = LightsailSearchService(session)
        db_ips = await svc.get_all_ips(config_id)
        db_by_name = {ip.static_ip_name: ip for ip in db_ips}

        # Add IPs present on AWS but missing from DB
        for name, ip_info in aws_by_name.items():
            if name not in db_by_name:
                logger.info("Reconcile %d: adding missing IP %s (%s) to DB",
                            config_id, name, ip_info["ipAddress"])
                await svc.add_static_ip(
                    config_id, name, ip_info["ipAddress"],
                    is_attached=ip_info["isAttached"],
                )

        # Remove IPs present in DB but no longer on AWS
        for name in db_by_name:
            if name not in aws_by_name:
                logger.info("Reconcile %d: removing stale IP %s from DB", config_id, name)
                await svc.delete_static_ip(name)

    logger.info("Reconcile %d: AWS=%d DB(before)=%d",
                config_id, len(aws_by_name), len(db_by_name))


async def _allocate_with_quota_recovery(
    config_id: int, region: str, ak: str, sk: str, new_name: str
) -> dict | None:
    """Try to allocate a static IP. On quota error: reconcile + release non-working + retry."""
    try:
        return await allocate_static_ip(region, ak, sk, new_name)
    except Exception as e:
        if "maximum number of static IPs" not in str(e):
            logger.error("Search %d: failed to allocate static IP: %s", config_id, e)
            return None

        logger.warning("Search %d: quota hit, reconciling and releasing non-working IPs", config_id)

        # Sync DB with AWS first
        await _reconcile_static_ips(config_id, region, ak, sk)

        # Release all non-working and untested IPs
        async with _session_factory() as session:
            all_ips = await LightsailSearchService(session).get_all_ips(config_id)
        candidates = [ip for ip in all_ips if ip.is_working is not True]

        if not candidates:
            logger.error("Search %d: quota hit but no releasable IPs found", config_id)
            return None

        for ip in candidates:
            try:
                await detach_static_ip(region, ak, sk, ip.static_ip_name)
                await release_static_ip(region, ak, sk, ip.static_ip_name)
                logger.info("Search %d: released %s (%s) to free quota",
                            config_id, ip.static_ip_name, ip.ip_address)
            except Exception as ex:
                logger.warning("Search %d: could not release %s: %s",
                               config_id, ip.static_ip_name, ex)
            async with _session_factory() as session:
                await LightsailSearchService(session).delete_static_ip(ip.static_ip_name)

        # Retry allocation
        try:
            return await allocate_static_ip(region, ak, sk, new_name)
        except Exception as e2:
            logger.error("Search %d: still can't allocate after quota recovery: %s", config_id, e2)
            return None


def _is_ip_working(
    reachable: bool | None, loss_pct: float | None, elapsed: float
) -> bool:
    if reachable is not True:
        return False
    if elapsed >= RELAX_AFTER_SECONDS:
        return loss_pct is None or loss_pct < LOSS_THRESHOLD_RELAXED
    return loss_pct is None or loss_pct <= LOSS_THRESHOLD_NORMAL
