"""Background scorer: Управляемый пул IP-адресов.

Периодически перепроверяет IP из пользовательских наборов через Pingachock:
  ping  — задержка и потери
  tls   — время TLS-хендшейка на kremnezar.online:443
  vless — работоспособность и скорость VPN через конфиг тега хостов

Итоговый score (0–100) определяет is_approved для каждого IP.
Запускается через asyncio.create_task() в main.py.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models.managed_pool import ManagedIp, ManagedPool
from services.ip_check_service import CHECK_BATCH, distributed_ping_check, normalize_addresses
from services.ip_set_service import IpSetService
from services.managed_pool_service import ManagedPoolService
from services.pingachock_api_service import PingachockAPIError, create_check, get_check, list_checks
from services.pingachock_service import PingachockService
from services.remnawave_api_service import RemnaWaveAPIError, get_vless_config_for_tag
from services.remnawave_service import RemnaWaveService

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60          # seconds between "which pools are due?" wakeups
TLS_TARGET = "kremnezar.online"
TLS_PORT = 443
SERVICE_TG_ID = 9636        # service user for VLESS config template

_scoring_pools: set[int] = set()   # pool IDs currently being scored (in-flight guard)


# ── scoring formula ───────────────────────────────────────────────────────────

def _ping_score(rtt_ms: float | None, loss_pct: float | None) -> float:
    """0–30 points for ping quality."""
    loss = (loss_pct or 0.0) * 100  # convert fraction → percentage
    if loss > 50:
        return 0.0
    if rtt_ms is None:
        return 10.0   # reachable but RTT unknown — neutral
    if rtt_ms > 300:
        base = 5.0
    elif rtt_ms > 150:
        base = 15.0
    elif rtt_ms > 80:
        base = 22.0
    else:
        base = 30.0
    # deduct 1pt per 5% loss
    return max(0.0, base - loss / 5)


def _tls_score(tls_ok: bool | None, tls_ms: float | None) -> float:
    """0–20 points for TLS handshake quality."""
    if not tls_ok:
        return 0.0
    if tls_ms is None:
        return 10.0   # TLS succeeded but no timing — neutral
    if tls_ms > 1000:
        return 5.0
    if tls_ms > 500:
        return 10.0
    if tls_ms > 200:
        return 15.0
    return 20.0


def _vless_score(speed_mbps: float | None) -> float:
    """0–50 points for VLESS connection speed."""
    if speed_mbps is None:
        return 0.0
    if speed_mbps < 1:
        return 5.0
    if speed_mbps < 5:
        return 15.0
    if speed_mbps < 15:
        return 30.0
    if speed_mbps < 30:
        return 40.0
    return 50.0


def compute_score(
    ping_reachable: bool,
    ping_rtt_ms: float | None,
    ping_loss_pct: float | None,
    tls_ok: bool | None,
    tls_handshake_ms: float | None,
    vless_ok: bool | None,
    vless_speed_mbps: float | None,
) -> float:
    """0–100 score for an IP. 0 if unreachable via ping."""
    if not ping_reachable:
        return 0.0
    if not vless_ok:
        # VLESS not working — partial credit for network quality only
        return _ping_score(ping_rtt_ms, ping_loss_pct) * 0.5 + _tls_score(tls_ok, tls_handshake_ms) * 0.25
    return (
        _ping_score(ping_rtt_ms, ping_loss_pct)
        + _tls_score(tls_ok, tls_handshake_ms)
        + _vless_score(vless_speed_mbps)
    )


# ── TLS batch check ───────────────────────────────────────────────────────────

async def _tls_check_batch(
    api_url: str,
    api_key: str,
    ips: list[str],
    poll_timeout: float = 90.0,
    poll_interval: float = 3.0,
) -> dict[str, tuple[bool, float | None]]:
    """TLS handshake check against TLS_TARGET:TLS_PORT for each IP.

    Pingachock TLS check uses the IP as the connection target and SNI for
    the domain name.  Returns {ip: (ok, handshake_ms_or_None)}.
    """
    if not ips:
        return {}
    try:
        resp = await create_check(
            api_url, api_key, "tls", {"all": True},
            targets=ips,
            params={"port": TLS_PORT, "sni": TLS_TARGET, "count": 2, "allow_insecure": True},
        )
    except PingachockAPIError as e:
        logger.warning("TLS batch check failed: %s", e)
        return {ip: (False, None) for ip in ips}

    id_to_ip: dict[str, str] = {c["id"]: c["target"] for c in resp.get("checks", [])}
    batch_id = resp.get("batch_id")
    pending = set(id_to_ip.keys())

    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_timeout
    while pending and loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            checks = await list_checks(api_url, api_key, batch_id=batch_id, limit=200)
        except PingachockAPIError:
            continue
        for c in checks:
            if c["id"] in pending and c.get("status") in (
                "completed", "partial", "failed", "cancelled"
            ):
                pending.discard(c["id"])

    fetches = await asyncio.gather(
        *[get_check(api_url, api_key, cid, expand="runs") for cid in id_to_ip],
        return_exceptions=True,
    )

    results: dict[str, tuple[bool, float | None]] = {}
    for cid, fetch in zip(id_to_ip.keys(), fetches):
        ip = id_to_ip[cid]
        if isinstance(fetch, Exception):
            results[ip] = (False, None)
            continue
        ok = fetch.get("status") in ("completed", "partial")
        rtt: float | None = None
        for run in fetch.get("runs", []):
            r = run.get("result") or {}
            for key in ("latency_ms", "handshake_ms", "duration_ms", "rtt_ms"):
                if r.get(key) is not None:
                    try:
                        rtt = float(r[key])
                    except (ValueError, TypeError):
                        pass
                    break
            if rtt is not None:
                break
        results[ip] = (ok, rtt)
    return results


# ── VLESS single-IP check ─────────────────────────────────────────────────────

async def _vless_check_ip(
    api_url: str,
    api_key: str,
    ip: str,
    vless_template: dict,
    poll_timeout: float = 120.0,
    poll_interval: float = 5.0,
) -> tuple[bool, float | None]:
    """VLESS speedtest for a single IP.

    Substitutes test IP into the template outbound and submits to Pingachock.
    Returns (ok, speed_mbps_or_None).
    """
    config = copy.deepcopy(vless_template)
    try:
        config["settings"]["vnext"][0]["address"] = ip
    except (KeyError, IndexError):
        logger.warning("VLESS template has unexpected structure, cannot set address")
        return False, None

    try:
        resp = await create_check(
            api_url, api_key, "vless", {"all": True},
            targets=[ip],
            params={"config": config},
        )
    except PingachockAPIError as e:
        logger.debug("VLESS check create failed for %s: %s", ip, e)
        return False, None

    check_id: str | None = None
    for c in resp.get("checks", []):
        check_id = c["id"]
        break
    if not check_id:
        return False, None

    batch_id = resp.get("batch_id")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_timeout
    while loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            checks = await list_checks(api_url, api_key, batch_id=batch_id, limit=50)
        except PingachockAPIError:
            continue
        for c in checks:
            if c["id"] == check_id and c.get("status") in (
                "completed", "partial", "failed", "cancelled"
            ):
                break
        else:
            continue
        break

    try:
        check_data = await get_check(api_url, api_key, check_id, expand="runs")
    except PingachockAPIError:
        return False, None

    ok = check_data.get("status") in ("completed", "partial")
    speed: float | None = None
    for run in check_data.get("runs", []):
        raw = (run.get("result") or {}).get("raw") or {}
        # Pingachock may report speed under various field names
        for key in ("download_mbps", "speed_mbps", "download_speed", "mbps", "speed"):
            val = raw.get(key)
            if val is not None:
                try:
                    speed = float(val)
                except (ValueError, TypeError):
                    pass
                break
        if speed is not None:
            break
    return ok, speed


# ── main pool scoring loop ────────────────────────────────────────────────────

async def run_ip_pool_scorer(session_factory: async_sessionmaker) -> None:
    """Entrypoint — run as asyncio.create_task() in main()."""
    logger.info("IP pool scorer started")
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("IP pool scorer cancelled")
            return
        try:
            due = await _get_due_pools(session_factory)
            for pool in due:
                if pool.id not in _scoring_pools:
                    asyncio.create_task(
                        _score_pool_safe(session_factory, pool.id),
                        name=f"pool-scorer-{pool.id}",
                    )
        except Exception:
            logger.exception("IP pool scorer: error in main loop")


async def _get_due_pools(session_factory: async_sessionmaker) -> list[ManagedPool]:
    """Return pools that are enabled and due for a re-scan."""
    async with session_factory() as session:
        svc = ManagedPoolService(session)
        pools = await svc.get_all_enabled()
    now = datetime.now(timezone.utc)
    due: list[ManagedPool] = []
    for p in pools:
        if p.last_scanned_at is None:
            due.append(p)
            continue
        last = p.last_scanned_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now >= last + timedelta(minutes=p.check_interval_minutes):
            due.append(p)
    return due


async def _score_pool_safe(session_factory: async_sessionmaker, pool_id: int) -> None:
    """Wrapper that guards against concurrent scoring of the same pool."""
    _scoring_pools.add(pool_id)
    try:
        await _score_pool(session_factory, pool_id)
    except Exception:
        logger.exception("IP pool scorer: error scoring pool %d", pool_id)
    finally:
        _scoring_pools.discard(pool_id)


async def _score_pool(session_factory: async_sessionmaker, pool_id: int) -> None:  # noqa: C901
    """Core scoring logic for one managed pool."""

    # ── 1. Load pool config ───────────────────────────────────────────────────
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        pool = await pool_svc.get_pool_any(pool_id)
        if not pool or not pool.enabled:
            return

        ip_svc = IpSetService(session)
        sets = await ip_svc.get_sets_by_ids(list(pool.ip_set_ids))

        rw_svc = RemnaWaveService(session)
        panels = await rw_svc.get_org_panels(pool.org_id)

        pc_svc = PingachockService(session)
        pc = await pc_svc.get_settings(pool.org_id)

    if not pc:
        logger.warning("Pool %d: Pingachock not configured for org %d", pool_id, pool.org_id)
        return
    if not panels:
        logger.warning("Pool %d: no Remnawave panels for org %d", pool_id, pool.org_id)
        return

    panel = panels[0]  # use first panel for VLESS config

    # ── 2. Build deduplicated IP list from source sets ────────────────────────
    seen_ips: set[str] = set()
    unique_ips: list[str] = []
    for s in sets:
        extracted, _ = normalize_addresses(s.addresses)
        for ip in extracted:
            if ip not in seen_ips:
                seen_ips.add(ip)
                unique_ips.append(ip)

    if not unique_ips:
        logger.info("Pool %d: source sets contain no valid IPs", pool_id)
        async with session_factory() as session:
            await ManagedPoolService(session).mark_scanned(pool_id)
        return

    logger.info("Pool %d: %d unique IPs from %d source sets", pool_id, len(unique_ips), len(sets))

    # ── 3. Sync managed_ips table (add new, remove stale) ────────────────────
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        removed = await pool_svc.remove_stale_ips(pool_id, set(unique_ips))
        if removed:
            logger.info("Pool %d: removed %d stale IPs", pool_id, removed)
        for ip in unique_ips:
            await pool_svc.upsert_ip(pool_id, ip)

    # ── 4. Get VLESS config template ──────────────────────────────────────────
    vless_template: dict | None = None
    try:
        vless_template = await get_vless_config_for_tag(
            panel.url, panel.api_token, SERVICE_TG_ID, pool.host_tag,
        )
        if vless_template:
            logger.debug("Pool %d: VLESS config template loaded for tag '%s'", pool_id, pool.host_tag)
        else:
            logger.warning(
                "Pool %d: no VLESS config found for tag '%s' (service user %d not found or tag has no hosts)",
                pool_id, pool.host_tag, SERVICE_TG_ID,
            )
    except RemnaWaveAPIError as e:
        logger.warning("Pool %d: could not get VLESS config: %s", pool_id, e)

    # ── 5. Fetch IPs that need checking ──────────────────────────────────────
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        pool_obj = await pool_svc.get_pool_any(pool_id)
        interval = pool_obj.check_interval_minutes if pool_obj else 120
        to_check = await pool_svc.get_ips_to_check(pool_id, interval)

    if not to_check:
        logger.info("Pool %d: all IPs are fresh, nothing to check", pool_id)
        async with session_factory() as session:
            await ManagedPoolService(session).mark_scanned(pool_id)
        return

    logger.info("Pool %d: checking %d/%d IPs", pool_id, len(to_check), len(unique_ips))
    check_ips = [m.ip for m in to_check]
    id_map = {m.id: m for m in to_check}

    # ── 6. Ping check (batched) ───────────────────────────────────────────────
    ping_results: dict[str, tuple[bool, float | None, float | None]] = {}
    for i in range(0, len(check_ips), CHECK_BATCH):
        batch = check_ips[i: i + CHECK_BATCH]
        try:
            batch_results = await distributed_ping_check(pc.api_url, pc.api_key, batch)
            ping_results.update(batch_results)
        except PingachockAPIError as e:
            logger.warning("Pool %d: ping batch %d failed: %s", pool_id, i // CHECK_BATCH, e)
            for ip in batch:
                ping_results[ip] = (False, None, None)

    # ── 7. TLS check (batched) ────────────────────────────────────────────────
    tls_results: dict[str, tuple[bool, float | None]] = {}
    for i in range(0, len(check_ips), CHECK_BATCH):
        batch = check_ips[i: i + CHECK_BATCH]
        batch_tls = await _tls_check_batch(pc.api_url, pc.api_key, batch)
        tls_results.update(batch_tls)

    # ── 8. VLESS check (sequential, only reachable IPs) ──────────────────────
    vless_results: dict[str, tuple[bool, float | None]] = {}
    if vless_template:
        reachable = [ip for ip in check_ips if ping_results.get(ip, (False,))[0]]
        for ip in reachable:
            vless_ok, speed = await _vless_check_ip(pc.api_url, pc.api_key, ip, vless_template)
            vless_results[ip] = (vless_ok, speed)
            logger.debug("Pool %d: %s vless=%s speed=%s Mbps", pool_id, ip, vless_ok, speed)
    else:
        logger.info("Pool %d: skipping VLESS checks (no template available)", pool_id)

    # ── 9. Compute scores and persist ─────────────────────────────────────────
    approved_count = 0
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        threshold_pool = await pool_svc.get_pool_any(pool_id)
        threshold = threshold_pool.score_threshold if threshold_pool else 60.0

        from sqlalchemy import select as _select
        for managed_ip in to_check:
            ip = managed_ip.ip
            ping_ok, ping_rtt, ping_loss = ping_results.get(ip, (False, None, None))
            tls_ok, tls_ms = tls_results.get(ip, (None, None))
            vless_ok, vless_speed = vless_results.get(ip, (None, None))

            score = compute_score(ping_ok, ping_rtt, ping_loss, tls_ok, tls_ms, vless_ok, vless_speed)
            approved = score >= threshold

            # Re-fetch object in the current session before updating
            result = await session.execute(
                _select(ManagedIp).where(ManagedIp.id == managed_ip.id)
            )
            fresh = result.scalar_one_or_none()
            if fresh:
                await pool_svc.update_ip_score(
                    fresh, score, approved,
                    ping_rtt, ping_loss, tls_ok, tls_ms, vless_ok, vless_speed,
                )
                if approved:
                    approved_count += 1

        await pool_svc.mark_scanned(pool_id)

    logger.info(
        "Pool %d: done — %d/%d approved (threshold %.0f)",
        pool_id, approved_count, len(check_ips), threshold,
    )
