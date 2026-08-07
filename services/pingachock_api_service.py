"""HTTP client for the Pingachock distributed availability-check API."""
from __future__ import annotations

import aiohttp


class PingachockAPIError(Exception):
    pass


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


async def get_nodes(api_url: str, api_key: str) -> list[dict]:
    """Return list of node dicts. Used to verify connectivity."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{api_url}/api/v1/nodes",
            headers=_headers(api_key),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise PingachockAPIError(f"HTTP {resp.status}: {text[:200]}")
            data = await resp.json()
            return data.get("nodes", [])


async def create_check(
    api_url: str,
    api_key: str,
    check_type: str,
    node_selector: dict,
    target: str | None = None,
    targets: list[str] | None = None,
    params: dict | None = None,
    callback_url: str | None = None,
) -> dict:
    """Create a check (single target) or batch (targets list).

    Returns Check or BatchCheckResponse dict.
    """
    payload: dict = {
        "type": check_type,
        "node_selector": node_selector,
    }
    if target is not None:
        payload["target"] = target
    if targets is not None:
        payload["targets"] = targets
    if params:
        payload["params"] = params
    if callback_url:
        payload["callback_url"] = callback_url

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{api_url}/api/v1/checks",
            headers=_headers(api_key),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise PingachockAPIError(f"HTTP {resp.status}: {text[:200]}")
            return await resp.json()


async def get_check(
    api_url: str,
    api_key: str,
    check_id: str,
    expand: str | None = None,
) -> dict:
    """Get check status + results. Pass expand='runs' for per-node detail."""
    params = {}
    if expand:
        params["expand"] = expand
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{api_url}/api/v1/checks/{check_id}",
            headers=_headers(api_key),
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise PingachockAPIError(f"HTTP {resp.status}: {text[:200]}")
            return await resp.json()


async def list_checks(
    api_url: str,
    api_key: str,
    batch_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    params: dict = {"limit": limit, "offset": offset}
    if batch_id:
        params["batch_id"] = batch_id
    if status:
        params["status"] = status
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{api_url}/api/v1/checks",
            headers=_headers(api_key),
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise PingachockAPIError(f"HTTP {resp.status}: {text[:200]}")
            data = await resp.json()
            return data.get("checks", [])


async def cancel_check(api_url: str, api_key: str, check_id: str) -> None:
    """Cancel a pending/running check."""
    async with aiohttp.ClientSession() as session:
        async with session.delete(
            f"{api_url}/api/v1/checks/{check_id}",
            headers=_headers(api_key),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (204, 404):
                text = await resp.text()
                raise PingachockAPIError(f"HTTP {resp.status}: {text[:200]}")


async def server_ping(
    api_url: str,
    api_key: str,
    targets: list[str],
    ports: list[str] | None = None,
) -> list[dict]:
    """Synchronous ping directly from the backend server (no node dispatch).

    Returns list of result dicts: {target, resolved_ip, icmp, ports}.
    ports items: 'icmp' and/or port numbers as strings.
    """
    payload: dict = {"targets": targets}
    if ports:
        payload["ports"] = ports
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{api_url}/api/v1/server-ping",
            headers=_headers(api_key),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise PingachockAPIError(f"HTTP {resp.status}: {text[:200]}")
            data = await resp.json()
            return data.get("results", [])
