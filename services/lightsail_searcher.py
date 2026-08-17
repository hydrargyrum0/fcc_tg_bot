"""Background service: Lightsail automatic IP search.

Search flow for one region config:
  1. Create a Lightsail nano Debian instance (if not already running).
  2. Open all ports (TCP+UDP full range + ICMP).
  3. Loop:
       a. If total_static_ips < 5: allocate a new static IP.
       b. If total_static_ips == 5 and need more: release one non-working IP,
          allocate new.
       c. Attach the new static IP to the instance (detach previous if needed).
       d. Wait 30s for propagation.
       e. Check via Pingachock (using config's node_ids).
       f. Record result. If working: add to found list.
       g. If working_count >= target_count → finish.
  4. Cleanup: delete non-working static IPs from Lightsail; delete instance.
  5. Mark config status: 'monitoring' if target reached, 'idle' otherwise.

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
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models.aws_account import AWSAccount
from db.models.lightsail_search import LightsailRegionConfig
from services.aws_service import AWSService
from services.ip_check_service import distributed_ping_check
from services.lightsail_api_service import (
    allocate_static_ip,
    attach_static_ip,
    create_instance,
    delete_instance,
    detach_static_ip,
    instance_exists,
    open_all_ports,
    release_static_ip,
    wait_for_instance_running,
)
from services.lightsail_search_service import LightsailSearchService
from services.pingachock_service import PingachockService, build_node_selector

logger = logging.getLogger(__name__)

MAX_STATIC_IPS = 5          # per-region limit
ATTACH_WAIT_SECONDS = 35    # time to wait after IP change before pinging
WAKEUP_INTERVAL = 60        # how often the scheduler wakes up (seconds)
LOSS_THRESHOLD_NORMAL = 0.0    # 0 % loss required initially
LOSS_THRESHOLD_RELAXED = 0.75  # after 1 hour: up to 75 % loss acceptable
RELAX_AFTER_SECONDS = 3600     # 1 hour

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
    """Launch a search task for config_id (idempotent)."""
    if is_running(config_id):
        return
    task = asyncio.create_task(
        _search_safe(config_id),
        name=f"lightsail-search-{config_id}",
    )
    _active_tasks[config_id] = task


async def stop_search(config_id: int) -> None:
    """Cancel a running search task and clean up the instance."""
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
                instance_name = cfg.instance_name
                aws = await AWSService(session).get_account_by_id(
                    cfg.aws_account_id, cfg.org_id
                )
                if aws:
                    try:
                        await delete_instance(
                            cfg.region, aws.access_key_id, aws.secret_access_key,
                            instance_name,
                        )
                    except Exception as e:
                        logger.warning("stop_search: could not delete instance: %s", e)
            if cfg:
                await svc.mark_paused(config_id)


# ── scheduler ─────────────────────────────────────────────────────────────────

async def run_lightsail_searcher(session_factory: async_sessionmaker, bot: Bot) -> None:
    """Entrypoint — run as asyncio.create_task() in main()."""
    init(session_factory, bot)
    logger.info("Lightsail searcher started")

    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("Lightsail searcher: unhandled error in tick")
        await asyncio.sleep(WAKEUP_INTERVAL)


async def _tick() -> None:
    """Resume any 'searching' configs that lost their task (e.g. after restart)."""
    if _session_factory is None:
        return
    async with _session_factory() as session:
        svc = LightsailSearchService(session)
        searching = await svc.get_all_searching()

    for cfg in searching:
        if not is_running(cfg.id):
            logger.info("Lightsail searcher: resuming config %d (%s)", cfg.id, cfg.region)
            await start_search(cfg.id)


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
        logger.error("Lightsail searcher not initialised")
        return

    # ── load config and credentials ──────────────────────────────────────────
    async with _session_factory() as session:
        svc = LightsailSearchService(session)
        cfg = await svc.get_config_by_id(config_id)
        if not cfg:
            return

        aws_svc = AWSService(session)
        aws = await aws_svc.get_account_by_id(cfg.aws_account_id, cfg.org_id)
        if not aws:
            logger.error("Search %d: AWS account %d not found", config_id, cfg.aws_account_id)
            await svc.set_status(config_id, "idle")
            return

        pc_svc = PingachockService(session)
        pc = await pc_svc.get_settings(cfg.org_id)
        if not pc:
            logger.error("Search %d: Pingachock not configured for org %d", config_id, cfg.org_id)
            await svc.set_status(config_id, "idle")
            return

    region = cfg.region
    ak = aws.access_key_id
    sk = aws.secret_access_key
    api_url = pc.api_url
    api_key = pc.api_key
    node_selector = build_node_selector(cfg)

    search_start = time.monotonic()

    # ── create / verify instance ─────────────────────────────────────────────
    instance_name = f"fcc-searcher-{config_id}"
    logger.info("Search %d [%s]: checking instance %s", config_id, region, instance_name)

    if not await instance_exists(region, ak, sk, instance_name):
        logger.info("Search %d: creating instance %s", config_id, instance_name)
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

    # record instance name in DB
    async with _session_factory() as session:
        await LightsailSearchService(session).mark_search_started(config_id, instance_name)

    logger.info("Search %d [%s]: instance ready, starting IP search", config_id, region)

    # ── main search loop ─────────────────────────────────────────────────────
    while True:
        # Reload config on each iteration to pick up status changes (pause/stop)
        async with _session_factory() as session:
            svc = LightsailSearchService(session)
            cfg = await svc.get_config_by_id(config_id)
            if not cfg or cfg.status not in ("searching",):
                logger.info("Search %d: status changed to %s, stopping", config_id,
                            cfg.status if cfg else "deleted")
                break
            target = cfg.target_count
            node_selector = build_node_selector(cfg)
            all_ips = await svc.get_all_ips(config_id)

        working = [ip for ip in all_ips if ip.is_working is True]
        non_working = [ip for ip in all_ips if ip.is_working is False]
        total = len(all_ips)

        logger.debug(
            "Search %d: total=%d working=%d non_working=%d target=%d",
            config_id, total, len(working), len(non_working), target,
        )

        if len(working) >= target:
            logger.info("Search %d: target %d reached, finishing", config_id, target)
            break

        # ── determine which IP to test next ───────────────────────────────
        if total < MAX_STATIC_IPS:
            # Allocate new static IP
            new_name = f"fcc-{config_id}-{uuid.uuid4().hex[:6]}"
            try:
                ip_info = await allocate_static_ip(region, ak, sk, new_name)
            except Exception as e:
                logger.error("Search %d: failed to allocate static IP: %s", config_id, e)
                await asyncio.sleep(30)
                continue

            new_ip_name = ip_info["name"]
            new_ip_addr = ip_info["ipAddress"]

            # Save to DB
            async with _session_factory() as session:
                await LightsailSearchService(session).add_static_ip(
                    config_id, new_ip_name, new_ip_addr, is_attached=False
                )
        else:
            # At 5 IPs: release one non-working to make room
            if not non_working:
                # All 5 are already working — shouldn't reach here, but safety
                break

            victim = non_working[0]
            logger.info("Search %d: rotating out non-working IP %s (%s)",
                        config_id, victim.static_ip_name, victim.ip_address)
            try:
                # Ensure it's detached before releasing
                await detach_static_ip(region, ak, sk, victim.static_ip_name)
                await release_static_ip(region, ak, sk, victim.static_ip_name)
            except Exception as e:
                logger.warning("Search %d: could not release %s: %s",
                               config_id, victim.static_ip_name, e)
            async with _session_factory() as session:
                await LightsailSearchService(session).delete_static_ip(victim.static_ip_name)

            # Allocate replacement
            new_name = f"fcc-{config_id}-{uuid.uuid4().hex[:6]}"
            try:
                ip_info = await allocate_static_ip(region, ak, sk, new_name)
            except Exception as e:
                logger.error("Search %d: failed to allocate static IP: %s", config_id, e)
                await asyncio.sleep(30)
                continue

            new_ip_name = ip_info["name"]
            new_ip_addr = ip_info["ipAddress"]

            async with _session_factory() as session:
                await LightsailSearchService(session).add_static_ip(
                    config_id, new_ip_name, new_ip_addr, is_attached=False
                )

        # ── detach any currently attached IP ──────────────────────────────
        currently_attached = [ip for ip in all_ips if ip.is_attached]
        for att in currently_attached:
            try:
                await detach_static_ip(region, ak, sk, att.static_ip_name)
            except Exception as e:
                logger.warning("Search %d: could not detach %s: %s",
                               config_id, att.static_ip_name, e)
            async with _session_factory() as session:
                await LightsailSearchService(session).set_ip_attached(
                    att.static_ip_name, False
                )

        # ── attach new IP ─────────────────────────────────────────────────
        try:
            await attach_static_ip(region, ak, sk, new_ip_name, instance_name)
        except Exception as e:
            logger.error("Search %d: failed to attach %s: %s", config_id, new_ip_name, e)
            # Mark as non-working and move on
            async with _session_factory() as session:
                await LightsailSearchService(session).set_ip_result(
                    new_ip_name, is_working=False
                )
            continue

        async with _session_factory() as session:
            await LightsailSearchService(session).set_ip_attached(new_ip_name, True)

        # ── wait for propagation ──────────────────────────────────────────
        logger.info("Search %d: waiting %ds for IP %s to propagate",
                    config_id, ATTACH_WAIT_SECONDS, new_ip_addr)
        await asyncio.sleep(ATTACH_WAIT_SECONDS)

        # ── Pingachock check ──────────────────────────────────────────────
        elapsed = time.monotonic() - search_start
        loss_ok = _check_loss_ok(elapsed)

        try:
            results = await distributed_ping_check(
                api_url, api_key, [new_ip_addr],
                node_selector=node_selector,
            )
            reachable, loss_pct, rtt_ms = results.get(new_ip_addr, (None, None, None))
        except Exception as e:
            logger.warning("Search %d: Pingachock error for %s: %s",
                           config_id, new_ip_addr, e)
            reachable, loss_pct, rtt_ms = None, None, None

        is_working = _is_ip_working(reachable, loss_pct, elapsed)

        parts = []
        if loss_pct is not None:
            parts.append(f"loss={loss_pct*100:.0f}%")
        if rtt_ms is not None:
            parts.append(f"rtt={rtt_ms:.0f}ms")
        logger.info(
            "Search %d: %s → %s (%s)",
            config_id, new_ip_addr,
            "✅ working" if is_working else "❌ not working",
            ", ".join(parts) if parts else "no stats",
        )

        # ── record result ─────────────────────────────────────────────────
        async with _session_factory() as session:
            svc = LightsailSearchService(session)
            await svc.set_ip_result(new_ip_name, is_working=is_working, is_attached=True)
            if is_working:
                # Detach working IP so it's free; keep it allocated
                pass   # keep is_attached=True until next iteration detaches it
            await svc.increment_checked(config_id)

    # ── cleanup ───────────────────────────────────────────────────────────────
    logger.info("Search %d: cleaning up", config_id)

    async with _session_factory() as session:
        svc = LightsailSearchService(session)
        # Get non-working IPs to release
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

    # Detach working IPs from instance (they stay allocated, not attached)
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

    # Delete instance
    try:
        await delete_instance(region, ak, sk, instance_name)
    except Exception as e:
        logger.warning("Search %d cleanup: could not delete instance: %s", config_id, e)

    async with _session_factory() as session:
        await LightsailSearchService(session).mark_search_stopped(config_id)

    logger.info("Search %d [%s]: finished", config_id, region)


def _is_ip_working(
    reachable: bool | None, loss_pct: float | None, elapsed: float
) -> bool:
    """Determine if an IP passes quality criteria given elapsed search time."""
    if reachable is not True:
        return False
    if elapsed >= RELAX_AFTER_SECONDS:
        # After 1 hour: accept any reachable IP with < 75% loss
        return loss_pct is None or loss_pct < LOSS_THRESHOLD_RELAXED
    else:
        # Initially: require 0% loss (or unknown)
        return loss_pct is None or loss_pct <= LOSS_THRESHOLD_NORMAL


def _check_loss_ok(elapsed: float) -> float:
    return LOSS_THRESHOLD_RELAXED if elapsed >= RELAX_AFTER_SECONDS else LOSS_THRESHOLD_NORMAL
