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

_scoring_pools: set[int] = set()   # pool IDs currently being scored (in-flight guard)


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
    logger.info(
        "Pool %d: checking %d/%d IPs in %d batches of %d",
        pool_id, len(check_ips), len(unique_ips), total_batches, POOL_SCORE_BATCH,
    )

    # ── 6–9. Per-batch: ping+TLS parallel → VLESS → commit → next batch ──────
    # Ping and TLS run in parallel (independent checks).
    # Scores are written after each batch so the UI fills up progressively.
    # VLESS speedtests run sequentially within each batch (heavy — one at a time).
    from sqlalchemy import select as _select
    approved_count = 0

    for batch_num, batch_start in enumerate(range(0, len(check_ips), POOL_SCORE_BATCH), 1):
        batch = check_ips[batch_start: batch_start + POOL_SCORE_BATCH]

        logger.debug(
            "Pool %d: batch %d/%d — %d IPs",
            pool_id, batch_num, total_batches, len(batch),
        )

        # Ping + TLS in parallel — independent, no need to wait for each other
        ping_res_or_exc, tls_res_or_exc = await asyncio.gather(
            distributed_ping_check(pc.api_url, pc.api_key, batch),
            _tls_check_batch(pc.api_url, pc.api_key, batch),
            return_exceptions=True,
        )

        if isinstance(ping_res_or_exc, Exception):
            logger.warning(
                "Pool %d: batch %d/%d ping error: %s", pool_id, batch_num, total_batches, ping_res_or_exc
            )
            ping_batch: dict[str, tuple[bool | None, float | None, float | None]] = {
                ip: (None, None, None) for ip in batch
            }
        else:
            ping_batch = ping_res_or_exc

        if isinstance(tls_res_or_exc, Exception):
            logger.warning(
                "Pool %d: batch %d/%d TLS error: %s", pool_id, batch_num, total_batches, tls_res_or_exc
            )
            tls_batch: dict[str, tuple[bool | None, float | None]] = {
                ip: (None, None) for ip in batch
            }
        else:
            tls_batch = tls_res_or_exc

        # Write scores for this batch immediately → UI fills up progressively
        async with session_factory() as session:
            pool_svc = ManagedPoolService(session)
            for ip in batch:
                tls_ok, tls_ms = tls_batch.get(ip, (None, None))
                if tls_ok is None:
                    # TLS timed out — don't know yet, preserve score, retry next cycle
                    logger.debug(
                        "Pool %d: skip %s — TLS timed out, preserving score", pool_id, ip
                    )
                    continue
                ping_ok, ping_rtt, ping_loss = ping_batch.get(ip, (None, None, None))
                score = compute_score(tls_ok, tls_ms, ping_loss, ping_rtt)
                approved = score >= threshold

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

    async with session_factory() as session:
        await ManagedPoolService(session).mark_scanned(pool_id)

    logger.info(
        "Pool %d: done — %d/%d approved (threshold %.0f)",
        pool_id, approved_count, len(check_ips), threshold,
    )
