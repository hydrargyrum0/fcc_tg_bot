"""Background scorer: Управляемый пул IP-адресов.

Периодически перепроверяет IP из пользовательских наборов через Pingachock:
  tls  — TLS-хендшейк на kremnezar.online:443 (ворота — без TLS IP не принимается)
  ping — задержка и потери (влияет на приоритет: меньше потерь = выше score)

Итоговый score (0–100) определяет is_approved для каждого IP.
Запускается через asyncio.create_task() в main.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models.managed_pool import ManagedIp, ManagedPool
from services.ip_check_service import (
    CHECK_BATCH, POLL_INTERVAL as _CHECK_POLL_INTERVAL, POLL_TIMEOUT,
    distributed_ping_check, normalize_addresses, poll_batch,
)
from services.ip_set_service import IpSetService
from services.managed_pool_service import ManagedPoolService
from services.pingachock_api_service import PingachockAPIError, create_check, get_check
from services.pingachock_service import PingachockService

logger = logging.getLogger(__name__)

SCORER_WAKEUP_INTERVAL = 60  # seconds between "which pools are due?" wakeups
TLS_TARGET = "kremnezar.online"
TLS_PORT = 443
POOL_SCORE_BATCH = 50        # IPs per Pingachock batch for pool scoring (vs CHECK_BATCH=5 for monitor)

_scoring_pools: set[int] = set()        # pool IDs currently being full-scored
_health_checking_pools: set[int] = set()  # pool IDs currently being health-checked
_health_check_times: dict[int, float] = {}  # pool_id → monotonic time of last health check


# ── scoring formula ───────────────────────────────────────────────────────────

def compute_score(
    tls_ok: bool | None,
    tls_handshake_ms: float | None,
    ping_loss_pct: float | None,
    ping_rtt_ms: float | None,
) -> float:
    """TLS is the gate — no TLS means score 0 (rejected).

    Range 0–100; default threshold 60 separates approved from rejected.
      • tls_ok = False / None  →  0   (confirmed fail or timeout)
      • tls_ok = True          →  base 70, +bonus for RTT, −penalty for loss
        - loss > 50 %          →  30  (below threshold — rejected)
        - 0 % loss, RTT < 80ms →  85
        - 0 % loss, any RTT    →  70–85
    """
    if not tls_ok:          # False or None (timed out — don't know yet)
        return 0.0
    loss = (ping_loss_pct or 0.0) * 100    # fraction 0–1 → percent 0–100
    if loss > 50:
        return 30.0         # very lossy — below default threshold
    loss_penalty = loss * 0.6              # 50 % → −30 pts
    rtt_bonus = 0.0
    if ping_rtt_ms is not None:
        if ping_rtt_ms < 80:
            rtt_bonus = 15.0
        elif ping_rtt_ms < 150:
            rtt_bonus = 10.0
        elif ping_rtt_ms < 300:
            rtt_bonus = 5.0
    return min(100.0, 70.0 - loss_penalty + rtt_bonus)


# ── TLS batch check ───────────────────────────────────────────────────────────

async def _tls_check_batch(
    api_url: str,
    api_key: str,
    ips: list[str],
    poll_timeout: float = POLL_TIMEOUT,
    poll_interval: float = _CHECK_POLL_INTERVAL,  # 5 s — same cadence as ping poller
) -> dict[str, tuple[bool | None, float | None]]:
    """TLS handshake check against TLS_TARGET:TLS_PORT for each IP.

    Pingachock TLS check uses the IP as the connection target and SNI for
    the domain name.  Returns {ip: (ok, handshake_ms_or_None)} where ok is
    None if the check timed out (unknown — do NOT treat as failed).
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
        return {ip: (None, None) for ip in ips}

    id_to_ip: dict[str, str] = {c["id"]: c["target"] for c in resp.get("checks", [])}
    batch_id = resp.get("batch_id")

    await poll_batch(api_url, api_key, batch_id, set(id_to_ip.keys()), poll_timeout, poll_interval)

    fetches = await asyncio.gather(
        *[get_check(api_url, api_key, cid, expand="runs") for cid in id_to_ip],
        return_exceptions=True,
    )

    results: dict[str, tuple[bool | None, float | None]] = {}
    for cid, fetch in zip(id_to_ip.keys(), fetches):
        ip = id_to_ip[cid]
        if isinstance(fetch, Exception):
            results[ip] = (False, None)
            continue
        status = fetch.get("status")
        if status not in ("completed", "partial", "failed", "cancelled"):
            results[ip] = (None, None)   # timed out — unknown, not failed
            continue
        ok = status in ("completed", "partial")
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


# ── main pool scoring loop ────────────────────────────────────────────────────

async def run_ip_pool_scorer(session_factory: async_sessionmaker) -> None:
    """Entrypoint — run as asyncio.create_task() in main()."""
    logger.info("IP pool scorer started")
    while True:
        try:
            await asyncio.sleep(SCORER_WAKEUP_INTERVAL)
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
            # Health checks: re-verify only approved IPs between full scans
            try:
                all_pools = await _get_all_enabled_pools(session_factory)
                for pool in all_pools:
                    if pool.id in _scoring_pools or pool.id in _health_checking_pools:
                        continue
                    health_interval_s = max(15, pool.check_interval_minutes // 3) * 60
                    last_hc = _health_check_times.get(pool.id, 0.0)
                    if time.monotonic() - last_hc >= health_interval_s:
                        asyncio.create_task(
                            _health_check_pool_safe(session_factory, pool.id),
                            name=f"pool-health-{pool.id}",
                        )
            except Exception:
                logger.exception("IP pool scorer: error in health-check loop")
        except Exception:
            logger.exception("IP pool scorer: error in main loop")


async def _get_all_enabled_pools(session_factory: async_sessionmaker) -> list[ManagedPool]:
    async with session_factory() as session:
        return await ManagedPoolService(session).get_all_enabled()


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


async def _health_check_pool_safe(session_factory: async_sessionmaker, pool_id: int) -> None:
    """Guard wrapper for health checks — prevents concurrent runs per pool."""
    _health_checking_pools.add(pool_id)
    _health_check_times[pool_id] = time.monotonic()
    try:
        await _health_check_pool(session_factory, pool_id)
    except Exception:
        logger.exception("IP pool scorer: error health-checking pool %d", pool_id)
    finally:
        _health_checking_pools.discard(pool_id)


async def _health_check_pool(session_factory: async_sessionmaker, pool_id: int) -> None:
    """Re-verify approved IPs only — runs between full scans.

    Faster than a full re-scan: checks only approved IPs, skips pending/rejected.
    Approved IPs that drop below threshold get their approval revoked; their
    metrics are always refreshed. Non-approved IPs are not touched (full scan
    handles those on the next cycle).
    """
    from sqlalchemy import select as _select

    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        pool = await pool_svc.get_pool_any(pool_id)
        if not pool or not pool.enabled:
            return
        approved_rows = await pool_svc.get_approved_ips(pool_id)
        pc_svc = PingachockService(session)
        pc = await pc_svc.get_settings(pool.org_id)
        threshold = pool.score_threshold

    if not pc:
        logger.warning("Pool %d health check: Pingachock not configured", pool_id)
        return

    if not approved_rows:
        logger.debug("Pool %d «%s»: health check — no approved IPs", pool_id, pool.name)
        return

    ips = [m.ip for m in approved_rows]
    total_batches = (len(ips) + POOL_SCORE_BATCH - 1) // POOL_SCORE_BATCH
    t0 = time.monotonic()
    logger.info(
        "Pool %d «%s»: health check — %d approved IPs, %d batches",
        pool_id, pool.name, len(ips), total_batches,
    )

    revoked = 0
    for batch_num, batch_start in enumerate(range(0, len(ips), POOL_SCORE_BATCH), 1):
        batch = ips[batch_start: batch_start + POOL_SCORE_BATCH]
        batch_t0 = time.monotonic()

        ping_res_or_exc, tls_res_or_exc = await asyncio.gather(
            distributed_ping_check(pc.api_url, pc.api_key, batch),
            _tls_check_batch(pc.api_url, pc.api_key, batch),
            return_exceptions=True,
        )
        ping_batch: dict[str, tuple[bool | None, float | None, float | None]] = (
            ping_res_or_exc if not isinstance(ping_res_or_exc, Exception)
            else {ip: (None, None, None) for ip in batch}
        )
        tls_batch: dict[str, tuple[bool | None, float | None]] = (
            tls_res_or_exc if not isinstance(tls_res_or_exc, Exception)
            else {ip: (None, None) for ip in batch}
        )

        b_revoked = b_kept = b_skipped = 0
        async with session_factory() as session:
            pool_svc = ManagedPoolService(session)
            for ip in batch:
                tls_ok, tls_ms = tls_batch.get(ip, (None, None))
                ping_ok, ping_loss, ping_rtt = ping_batch.get(ip, (None, None, None))

                if tls_ok is None:
                    b_skipped += 1
                    continue  # TLS timeout — preserve approval, retry on next cycle

                score = compute_score(tls_ok, tls_ms, ping_loss, ping_rtt)
                now_approved = score >= threshold

                result = await session.execute(
                    _select(ManagedIp).where(
                        ManagedIp.pool_id == pool_id, ManagedIp.ip == ip,
                    )
                )
                managed = result.scalar_one_or_none()
                if not managed:
                    continue

                was_approved = managed.is_approved   # save before update mutates it
                await pool_svc.update_ip_score(
                    managed, score, now_approved,
                    ping_rtt, ping_loss, tls_ok, tls_ms,
                    vless_ok=None, vless_speed_mbps=None,
                )

                if was_approved and not now_approved:
                    b_revoked += 1
                    revoked += 1
                    logger.debug(
                        "Pool %d: %s — health REVOKED (score=%.0f, TLS=%s, RTT=%s, loss=%s)",
                        pool_id, ip, score,
                        "OK" if tls_ok else "FAIL",
                        f"{ping_rtt:.0f}ms" if ping_rtt is not None else "?",
                        f"{ping_loss * 100:.0f}%" if ping_loss is not None else "?",
                    )
                else:
                    b_kept += 1

        logger.debug(
            "Pool %d: health batch %d/%d [%.0fs] — kept %d, revoked %d, skipped %d",
            pool_id, batch_num, total_batches,
            time.monotonic() - batch_t0, b_kept, b_revoked, b_skipped,
        )

    elapsed = time.monotonic() - t0
    m, s = divmod(int(elapsed), 60)
    logger.info(
        "Pool %d «%s»: health check done in %dm%02ds — %d/%d approved (%d revoked)",
        pool_id, pool.name, m, s, len(ips) - revoked, len(ips), revoked,
    )


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

        pc_svc = PingachockService(session)
        pc = await pc_svc.get_settings(pool.org_id)

    if not pc:
        logger.warning("Pool %d: Pingachock not configured for org %d", pool_id, pool.org_id)
        return

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
        await session.commit()  # flush() inside upsert_ip is not enough — must commit

    # ── 4. Fetch IPs that need checking ──────────────────────────────────────
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        pool_obj = await pool_svc.get_pool_any(pool_id)
        interval = pool_obj.check_interval_minutes if pool_obj else 120
        threshold = pool_obj.score_threshold if pool_obj else 60.0
        to_check = await pool_svc.get_ips_to_check(pool_id, interval)

    if not to_check:
        logger.info("Pool %d: all IPs are fresh, nothing to check", pool_id)
        async with session_factory() as session:
            await ManagedPoolService(session).mark_scanned(pool_id)
        return

    check_ips = [m.ip for m in to_check]
    total_batches = (len(check_ips) + POOL_SCORE_BATCH - 1) // POOL_SCORE_BATCH
    scorer_start = time.monotonic()
    logger.info(
        "Pool %d «%s»: starting — %d/%d IPs to check, %d batches of ≤%d, threshold=%.0f",
        pool_id, pool.name, len(check_ips), len(unique_ips),
        total_batches, POOL_SCORE_BATCH, threshold,
    )

    # ── 6–9. Per-batch: ping+TLS parallel → commit → next batch ──────────────
    # Ping and TLS run in parallel (independent checks).
    # Scores are written after each batch so the UI fills up progressively.
    from sqlalchemy import select as _select
    approved_count = 0
    skipped_count  = 0   # TLS timed out

    for batch_num, batch_start in enumerate(range(0, len(check_ips), POOL_SCORE_BATCH), 1):
        batch = check_ips[batch_start: batch_start + POOL_SCORE_BATCH]
        batch_t0 = time.monotonic()

        # Ping + TLS in parallel — independent, no need to wait for each other
        ping_res_or_exc, tls_res_or_exc = await asyncio.gather(
            distributed_ping_check(pc.api_url, pc.api_key, batch),
            _tls_check_batch(pc.api_url, pc.api_key, batch),
            return_exceptions=True,
        )

        if isinstance(ping_res_or_exc, Exception):
            logger.warning(
                "Pool %d: batch %d/%d — ping RPC failed: %s",
                pool_id, batch_num, total_batches, ping_res_or_exc,
            )
            ping_batch: dict[str, tuple[bool | None, float | None, float | None]] = {
                ip: (None, None, None) for ip in batch
            }
        else:
            ping_batch = ping_res_or_exc

        if isinstance(tls_res_or_exc, Exception):
            logger.warning(
                "Pool %d: batch %d/%d — TLS RPC failed: %s",
                pool_id, batch_num, total_batches, tls_res_or_exc,
            )
            tls_batch: dict[str, tuple[bool | None, float | None]] = {
                ip: (None, None) for ip in batch
            }
        else:
            tls_batch = tls_res_or_exc

        # Per-batch counters for the summary log line
        b_tls_ok = b_tls_fail = b_tls_timeout = 0
        b_ping_ok = b_ping_fail = b_ping_timeout = 0
        b_approved = b_rejected = b_skipped = 0

        # Write scores for this batch immediately → UI fills up progressively
        async with session_factory() as session:
            pool_svc = ManagedPoolService(session)
            for ip in batch:
                tls_ok, tls_ms = tls_batch.get(ip, (None, None))
                # distributed_ping_check returns (reachable, loss_pct, avg_rtt_ms)
                ping_ok, ping_loss, ping_rtt = ping_batch.get(ip, (None, None, None))

                # Count ping result
                if ping_ok is True:   b_ping_ok += 1
                elif ping_ok is False: b_ping_fail += 1
                else:                 b_ping_timeout += 1

                # Count TLS result
                if tls_ok is True:   b_tls_ok += 1
                elif tls_ok is False: b_tls_fail += 1
                else:                 b_tls_timeout += 1

                if tls_ok is None:
                    # TLS timed out — preserve existing score, retry next cycle
                    b_skipped += 1
                    skipped_count += 1
                    logger.debug("Pool %d: %s — TLS timeout, score preserved", pool_id, ip)
                    continue

                score = compute_score(tls_ok, tls_ms, ping_loss, ping_rtt)
                approved = score >= threshold

                # Debug line per IP (only visible at DEBUG log level)
                logger.debug(
                    "Pool %d: %s — TLS=%s(%s) ping=%s(RTT=%s loss=%s) → score=%.0f %s",
                    pool_id, ip,
                    "OK" if tls_ok else "FAIL",
                    f"{tls_ms:.0f}ms" if tls_ms is not None else "?",
                    "OK" if ping_ok else ("FAIL" if ping_ok is False else "?"),
                    f"{ping_rtt:.0f}ms" if ping_rtt is not None else "?",
                    f"{ping_loss * 100:.0f}%" if ping_loss is not None else "?",
                    score,
                    "✓ APPROVED" if approved else "✗ REJECTED",
                )

                result = await session.execute(
                    _select(ManagedIp).where(
                        ManagedIp.pool_id == pool_id, ManagedIp.ip == ip
                    )
                )
                managed = result.scalar_one_or_none()
                if managed:
                    await pool_svc.update_ip_score(
                        managed, score, approved,
                        ping_rtt, ping_loss, tls_ok, tls_ms,
                        vless_ok=None, vless_speed_mbps=None,
                    )
                    if approved:
                        approved_count += 1
                        b_approved += 1
                    else:
                        b_rejected += 1

        elapsed_batch = time.monotonic() - batch_t0
        logger.info(
            "Pool %d: batch %d/%d [%.0fs] — "
            "ping: %d✓ %d✗ %d? | TLS: %d✓ %d✗ %d? | approved: %d/%d",
            pool_id, batch_num, total_batches, elapsed_batch,
            b_ping_ok, b_ping_fail, b_ping_timeout,
            b_tls_ok, b_tls_fail, b_tls_timeout,
            b_approved, len(batch) - b_skipped,
        )

    async with session_factory() as session:
        await ManagedPoolService(session).mark_scanned(pool_id)

    elapsed_total = time.monotonic() - scorer_start
    m, s = divmod(int(elapsed_total), 60)
    logger.info(
        "Pool %d «%s»: done in %dm%02ds — "
        "%d/%d approved (threshold %.0f)%s",
        pool_id, pool.name, m, s,
        approved_count, len(check_ips), threshold,
        f", {skipped_count} skipped (TLS timeout, will retry)" if skipped_count else "",
    )
