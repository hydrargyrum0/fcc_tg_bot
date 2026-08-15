"""Shared utilities for IP address checking via Pingachock distributed nodes.

Used by both hosts_fsm (interactive) and availability_monitor (background).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re as _re

from services.pingachock_api_service import (
    PingachockAPIError,
    create_check,
    get_check,
    list_checks,
)

logger = logging.getLogger(__name__)

CHECK_BATCH = 5      # IPs per Pingachock distributed-check batch
POLL_TIMEOUT = 900.0  # 15 min — background tasks have no external deadline
POLL_INTERVAL = 5.0   # seconds between Pingachock status polls

# TLS check defaults — used by tls_check_batch() and pool scorer
TLS_CHECK_HOST = "kremnezar.online"
TLS_CHECK_PORT = 443

# ── automation priority ───────────────────────────────────────────────────────

_automation_active: set[int] = set()   # group IDs with checks currently in-flight


async def wait_for_automation_priority() -> None:
    """Pool scoring and health checks call this between batches.

    If automation group checks are in-flight, yields the event loop so they
    get to run without competing for Pingachock API quota with a large batch.
    """
    if _automation_active:
        logger.debug(
            "Pool scoring yielding — %d automation check(s) active: %s",
            len(_automation_active), _automation_active,
        )
        await asyncio.sleep(0)

# Matches bare IPv4, IPv4/CIDR, bare IPv6, IPv6/CIDR embedded in any text
_IP_PATTERN = _re.compile(
    r'\b(?:'
    r'(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?'              # IPv4 (optional CIDR)
    r'|(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?:/\d{1,3})?'  # IPv6
    r')\b'
)


def normalize_addresses(text: str, cap: int | None = None) -> tuple[list[str], int]:
    """Extract and expand all IP/CIDR entries from arbitrary text.

    Ignores surrounding garbage (comments, JSON keys, markdown, etc.).
    Deduplicates results. CIDRs are expanded to individual host addresses.

    Returns (ips, skipped_count) where skipped_count is regex-matched fragments
    that failed ipaddress validation (e.g. "192.168.1.999").
    """
    raw_matches = _IP_PATTERN.findall(text)
    ips: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for entry in raw_matches:
        try:
            net = ipaddress.ip_network(entry, strict=False)
            if net.num_addresses == 1:
                ip = str(net.network_address)
                if ip not in seen:
                    seen.add(ip)
                    ips.append(ip)
                    if cap and len(ips) >= cap:
                        return ips, skipped
            else:
                for host in net.hosts():
                    ip = str(host)
                    if ip not in seen:
                        seen.add(ip)
                        ips.append(ip)
                        if cap and len(ips) >= cap:
                            return ips, skipped
        except ValueError:
            skipped += 1
    return ips, skipped


def expand_addresses(addresses_text: str, cap: int | None = None) -> tuple[list[str], bool]:
    """Expand IP set text (may contain CIDRs) into bare IP strings.

    Returns (ips, was_truncated).  /32 → single IP, /24 → 254 IPs, etc.
    IPv6 CIDRs are expanded the same way.
    """
    ips: list[str] = []
    truncated = False
    for line in addresses_text.splitlines():
        entry = line.strip()
        if not entry:
            continue
        try:
            net = ipaddress.ip_network(entry, strict=False)
            if net.num_addresses == 1:
                ips.append(str(net.network_address))
            else:
                for host in net.hosts():
                    ips.append(str(host))
                    if cap and len(ips) >= cap:
                        return ips, True
        except ValueError:
            try:
                ips.append(str(ipaddress.ip_address(entry)))
            except ValueError:
                pass
        if cap and len(ips) >= cap:
            return ips, True
    return ips, truncated


async def poll_batch(
    api_url: str,
    api_key: str,
    batch_id: str | None,
    check_ids: set[str],
    poll_timeout: float = POLL_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
) -> dict[str, str]:
    """Poll Pingachock until all check IDs reach a terminal state or timeout.

    Returns {check_id: status}:
    - "completed" / "partial" / "failed" / "cancelled" — finished checks
    - "pending" / "running" (last seen) — checks still in-progress at timeout

    Callers must treat non-terminal statuses as **unknown**, never as "failed".
    """
    pending = set(check_ids)
    statuses: dict[str, str] = {cid: "pending" for cid in check_ids}

    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_timeout
    while pending and loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            checks = await list_checks(api_url, api_key, batch_id=batch_id, limit=200)
        except PingachockAPIError:
            continue
        for c in checks:
            cid = c["id"]
            if cid not in pending:
                continue
            status = c.get("status", "")
            statuses[cid] = status
            if status in ("completed", "partial", "failed", "cancelled"):
                pending.discard(cid)
    return statuses


async def distributed_check(
    api_url: str,
    api_key: str,
    ips: list[str],
    *,
    poll_timeout: float = POLL_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
) -> dict[str, bool | None]:
    """Check IPs via real TM Pingachock nodes (ICMP ping + TCP 443).

    Fires two batch checks concurrently (ping and tcp), then polls until all
    complete or timeout.  Returns:
      True  — at least one node reached the IP via ping or TCP:443
      False — all checks finished and none succeeded
      None  — at least one check timed out (unknown — do NOT treat as failed)
    """
    if not ips:
        return {}

    try:
        ping_resp, tcp_resp = await asyncio.gather(
            create_check(
                api_url, api_key, "ping", {"all": True},
                targets=ips,
                params={"count": 3, "timeout_ms": 3000},
            ),
            create_check(
                api_url, api_key, "tcp", {"all": True},
                targets=ips,
                params={"port": 443, "timeout_ms": 3000},
            ),
        )
    except PingachockAPIError:
        raise

    id_to_ip: dict[str, str] = {}
    pending_ids: set[str] = set()
    batch_ids: list[str] = []

    for resp in (ping_resp, tcp_resp):
        bid = resp.get("batch_id")
        if bid:
            batch_ids.append(bid)
        for c in resp.get("checks", []):
            id_to_ip[c["id"]] = c["target"]
            pending_ids.add(c["id"])

    # None = unknown (check still pending at timeout)
    # False = tentative (may be upgraded to True by the other check type)
    # True  = confirmed reachable
    ip_ok: dict[str, bool | None] = {ip: None for ip in ips}

    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_timeout

    while pending_ids and loop.time() < deadline:
        await asyncio.sleep(poll_interval)

        for batch_id in batch_ids:
            try:
                checks = await list_checks(api_url, api_key, batch_id=batch_id, limit=200)
            except PingachockAPIError:
                continue

            for c in checks:
                cid = c["id"]
                if cid not in pending_ids:
                    continue
                status = c.get("status", "")
                if status in ("completed", "partial", "failed", "cancelled"):
                    pending_ids.discard(cid)
                    ip = id_to_ip.get(cid)
                    if not ip:
                        continue
                    if status in ("completed", "partial"):
                        ip_ok[ip] = True          # any success wins
                    elif ip_ok[ip] is None:
                        ip_ok[ip] = False         # tentative; other check may override

    # IPs still in pending_ids hit the timeout → remain None (unknown)
    return ip_ok


# ── node exclusion ────────────────────────────────────────────────────────────

EXCLUDED_NODES: frozenset[str] = frozenset({"server"})
"""Pingachock node names to exclude from all check results.

The 'server' node runs pings directly from the Pingachock backend host, which
has unrestricted network access and reports IPs as reachable even when real
distributed nodes cannot reach them.  Excluding it ensures reachability and
quality metrics reflect actual user-facing conditions.
"""


def run_node_name(run: dict) -> str:
    """Extract lower-cased node name from a check-run dict."""
    node = run.get("node") or run.get("node_name") or ""
    if isinstance(node, dict):
        return str(node.get("name") or node.get("id") or "").lower()
    return str(node).lower()


def _ping_run_ok(run: dict) -> bool:
    """True if a done ping run shows the target was reachable."""
    raw = (run.get("result") or {}).get("raw") or {}
    recv = raw.get("packets_recv") or raw.get("received") or raw.get("PacketsRecv")
    if recv is not None:
        return int(recv) > 0
    # No packet counts — positive RTT implies success
    for key in ("avg_rtt", "rtt_avg", "avg_ms", "AvgRtt", "rtt"):
        v = raw.get(key)
        if v is not None:
            try:
                if float(v) > 0:
                    return True
            except (ValueError, TypeError):
                pass
    return False


async def tls_check_batch(
    api_url: str,
    api_key: str,
    ips: list[str],
    tls_host: str = TLS_CHECK_HOST,
    tls_port: int = TLS_CHECK_PORT,
    poll_timeout: float = POLL_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
) -> dict[str, tuple[bool | None, float | None]]:
    """TLS handshake check for each IP against tls_host:tls_port.

    Returns {ip: (ok, handshake_ms_or_None)} where:
      ok = True  — handshake completed (at least one distributed node)
      ok = False — handshake confirmed failed on all included nodes
      ok = None  — timed out or only excluded nodes responded (unknown)
    """
    if not ips:
        return {}
    try:
        resp = await create_check(
            api_url, api_key, "tls", {"all": True},
            targets=ips,
            params={"port": tls_port, "sni": tls_host, "count": 2, "allow_insecure": True},
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

        # Exclude runs from the "server" backend node
        all_runs = fetch.get("runs", [])
        work_runs = [r for r in all_runs if run_node_name(r) not in EXCLUDED_NODES]
        if not work_runs:
            results[ip] = (None, None)   # only "server" responded — unknown
            continue

        _ACTIVE_RUN_STATUSES = frozenset({"pending", "running", "queued"})
        done_runs = [r for r in work_runs if r.get("status") not in _ACTIVE_RUN_STATUSES]

        logger.debug(
            "TLS %s: work_runs: %s",
            ip,
            [(run_node_name(r), r.get("status")) for r in work_runs],
        )

        rtt: float | None = None
        for run in done_runs:
            r = run.get("result") or {}
            for key in ("latency_ms", "handshake_ms", "duration_ms", "rtt_ms"):
                v = r.get(key)
                if v is not None:
                    try:
                        rtt = float(v)
                    except (ValueError, TypeError):
                        pass
                    break
            if rtt is not None:
                break

        if done_runs:
            ok: bool | None = rtt is not None and rtt > 0
        else:
            ok = None   # no included runs completed

        results[ip] = (ok, rtt)
    return results


async def distributed_ping_check(
    api_url: str,
    api_key: str,
    ips: list[str],
    *,
    count: int = 4,
    poll_timeout: float = POLL_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
) -> dict[str, tuple[bool | None, float | None, float | None]]:
    """ICMP-only ping, single distributed check.

    Returns {ip: (reachable, loss_pct, avg_rtt_ms)} where:
      reachable  — True/False if check completed; None if check timed out (unknown)
      loss_pct   — fraction 0.0–1.0 of packets lost; None if no node reported
                   raw packet data (treat unknown as acceptable, not penalise)
      avg_rtt_ms — average RTT in milliseconds; None if unavailable
    """
    if not ips:
        return {}

    resp = await create_check(
        api_url, api_key, "ping", {"all": True},
        targets=ips,
        params={"count": count, "timeout_ms": 3000},
    )

    id_to_ip: dict[str, str] = {}
    for c in resp.get("checks", []):
        id_to_ip[c["id"]] = c["target"]

    if not id_to_ip:
        return {ip: (None, None, None) for ip in ips}

    batch_id = resp.get("batch_id")
    await poll_batch(api_url, api_key, batch_id, set(id_to_ip.keys()), poll_timeout, poll_interval)

    # Fetch all check results in parallel — one HTTP request per check ID.
    check_ids = list(id_to_ip.keys())
    raw_fetches = await asyncio.gather(
        *[get_check(api_url, api_key, cid, expand="runs") for cid in check_ids],
        return_exceptions=True,
    )

    results: dict[str, tuple[bool | None, float | None, float | None]] = {}
    for check_id, fetch_result in zip(check_ids, raw_fetches):
        ip = id_to_ip[check_id]
        if isinstance(fetch_result, Exception):
            results[ip] = (False, None, None)
            continue

        check_data: dict = fetch_result
        status = check_data.get("status")
        if status not in ("completed", "partial", "failed", "cancelled"):
            # Check timed out — we genuinely don't know; do NOT treat as failure
            results[ip] = (None, None, None)
            continue

        # Only use runs from non-excluded nodes.
        # "server" runs from the backend host and sees all IPs as reachable,
        # which would mask real failures on distributed nodes.
        all_runs = check_data.get("runs", [])
        work_runs = [r for r in all_runs if run_node_name(r) not in EXCLUDED_NODES]
        if not work_runs:
            # No distributed-node runs (all excluded) — result is unknown
            logger.debug(
                "IP %s: check %s completed but all runs are from excluded nodes %s "
                "(nodes: %s)",
                ip, check_id, EXCLUDED_NODES,
                [(run_node_name(r), r.get("status")) for r in all_runs],
            )
            results[ip] = (None, None, None)
            continue

        # Derive reachability from finished runs.
        # Accept any terminal state — Pingachock uses "done" for successful runs
        # but may use "failed", "error", "timeout", etc. for unreachable nodes.
        # Explicitly skip only actively-running states.
        _ACTIVE_RUN_STATUSES = frozenset({"pending", "running", "queued"})
        done_runs = [r for r in work_runs if r.get("status") not in _ACTIVE_RUN_STATUSES]

        logger.debug(
            "IP %s: check %s — work_runs: %s",
            ip, check_id,
            [(run_node_name(r), r.get("status")) for r in work_runs],
        )

        if done_runs:
            reachable: bool | None = any(_ping_run_ok(r) for r in done_runs)
        else:
            # work_runs exist (TM nodes were assigned) but none reached terminal
            # state even after the check was marked "completed".  This happens
            # when the check completes because the "server" node finished quickly
            # while TM nodes are still awaiting ICMP responses from a blocked IP.
            # Treat as unreachable rather than unknown — a stuck TM run means the
            # IP is effectively unreachable from distributed nodes.
            logger.info(
                "IP %s: check %s — all work_runs still active after check completed "
                "(nodes: %s); treating as DEAD",
                ip, check_id,
                [(run_node_name(r), r.get("status")) for r in work_runs],
            )
            reachable = False

        total_sent = 0
        total_recv = 0
        total_rtt_sum = 0.0
        rtt_count = 0

        for run in work_runs:
            if run.get("status") in _ACTIVE_RUN_STATUSES:
                continue
            result = run.get("result") or {}
            raw = result.get("raw") or {}

            # Packet counts — only from nodes that report raw data.
            # No fallback: if a node doesn't send packets, skip its loss data.
            sent = (raw.get("packets_sent") or raw.get("sent") or raw.get("PacketsSent"))
            recv = (raw.get("packets_recv") or raw.get("received") or
                    raw.get("packets_received") or raw.get("PacketsRecv"))
            if sent is not None and recv is not None:
                total_sent += int(sent)
                total_recv += int(recv)

            # RTT — try common field name conventions.
            # Go time.Duration is nanoseconds; values ≥ 1_000_000 are converted to ms.
            rtt_raw = (
                raw.get("avg_rtt") or raw.get("rtt_avg") or raw.get("avg_ms") or
                raw.get("AvgRtt") or raw.get("rtt") or raw.get("avg_latency") or
                raw.get("latency_ms") or raw.get("latency")
            )
            if rtt_raw is not None:
                try:
                    rtt_val = float(rtt_raw)
                    if rtt_val >= 1_000_000:
                        rtt_val /= 1_000_000   # nanoseconds → ms
                    elif rtt_val >= 1_000:
                        rtt_val /= 1_000       # microseconds → ms
                    if rtt_val > 0:
                        total_rtt_sum += rtt_val
                        rtt_count += 1
                except (ValueError, TypeError):
                    pass

        # loss_pct: None when no node reported raw packet data
        loss_pct: float | None = None
        if total_sent > 0:
            loss_pct = max(0.0, 1.0 - total_recv / total_sent)

        # avg_rtt_ms: None when no node reported RTT
        avg_rtt_ms: float | None = None
        if rtt_count > 0:
            avg_rtt_ms = total_rtt_sum / rtt_count

        results[ip] = (reachable, loss_pct, avg_rtt_ms)

    return results


async def find_first_working_ip(
    api_url: str,
    api_key: str,
    pool: list[str],
    is_cancelled_fn=None,
) -> str | None:
    """Find first working IP from pool, checking CHECK_BATCH at a time.

    is_cancelled_fn: callable() -> bool — returns True if search should stop.
    Returns working IP or None if pool exhausted or cancelled.
    All IPs in pool are checked via Pingachock before returning.
    """
    offset = 0
    while offset < len(pool):
        if is_cancelled_fn and is_cancelled_fn():
            return None
        batch = pool[offset: offset + CHECK_BATCH]
        offset += CHECK_BATCH
        try:
            results = await distributed_check(api_url, api_key, batch)
        except PingachockAPIError:
            continue
        for ip in batch:
            if results.get(ip):
                return ip
    return None
