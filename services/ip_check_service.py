"""Shared utilities for IP address checking via Pingachock distributed nodes.

Used by both hosts_fsm (interactive) and availability_monitor (background).
"""
from __future__ import annotations

import asyncio
import ipaddress

from services.pingachock_api_service import (
    PingachockAPIError,
    create_check,
    list_checks,
)

CHECK_BATCH = 5  # IPs per Pingachock distributed-check batch


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

    loop = asyncio.get_event_loop()
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
