"""Shared utilities for IP address checking via Pingachock distributed nodes.

Used by both hosts_fsm (interactive) and availability_monitor (background).
"""
from __future__ import annotations

import asyncio
import ipaddress
import re as _re

from services.pingachock_api_service import (
    PingachockAPIError,
    create_check,
    get_check,
    list_checks,
)

CHECK_BATCH = 5  # IPs per Pingachock distributed-check batch

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


async def distributed_check(
    api_url: str,
    api_key: str,
    ips: list[str],
    *,
    poll_timeout: float = 90.0,
    poll_interval: float = 3.0,
) -> dict[str, bool]:
    """Check IPs via real TM Pingachock nodes (ICMP ping + TCP 443).

    Fires two batch checks concurrently (ping and tcp), then polls until
    all complete or timeout. Returns {ip: True} if at least one TM node
    could reach the IP via either ICMP or TCP:443.
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

    ip_ok: dict[str, bool] = {ip: False for ip in ips}

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
                    if status in ("completed", "partial"):
                        ip = id_to_ip.get(cid)
                        if ip:
                            ip_ok[ip] = True

    return ip_ok


async def distributed_ping_check(
    api_url: str,
    api_key: str,
    ips: list[str],
    *,
    count: int = 4,
    poll_timeout: float = 90.0,
    poll_interval: float = 3.0,
) -> dict[str, tuple[bool, float | None, float | None]]:
    """ICMP-only ping, single distributed check.

    Returns {ip: (reachable, loss_pct, avg_rtt_ms)} where:
      reachable  — True if check status is completed / partial
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
        return {ip: (False, None, None) for ip in ips}

    batch_id = resp.get("batch_id")
    pending_ids: set[str] = set(id_to_ip.keys())

    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_timeout
    while pending_ids and loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            checks = await list_checks(api_url, api_key, batch_id=batch_id, limit=200)
        except PingachockAPIError:
            continue
        for c in checks:
            if c["id"] in pending_ids and c.get("status") in (
                "completed", "partial", "failed", "cancelled"
            ):
                pending_ids.discard(c["id"])

    # Fetch all check results in parallel — one HTTP request per check ID.
    check_ids = list(id_to_ip.keys())
    raw_fetches = await asyncio.gather(
        *[get_check(api_url, api_key, cid, expand="runs") for cid in check_ids],
        return_exceptions=True,
    )

    results: dict[str, tuple[bool, float | None, float | None]] = {}
    for check_id, fetch_result in zip(check_ids, raw_fetches):
        ip = id_to_ip[check_id]
        if isinstance(fetch_result, Exception):
            results[ip] = (False, None, None)
            continue

        check_data: dict = fetch_result
        reachable = check_data.get("status") in ("completed", "partial")
        total_sent = 0
        total_recv = 0
        total_rtt_sum = 0.0
        rtt_count = 0

        for run in check_data.get("runs", []):
            if run.get("status") != "done":
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
