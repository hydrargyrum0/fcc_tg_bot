import aiohttp

CF_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareAPIError(Exception):
    pass


def _headers(email: str, api_key: str) -> dict:
    return {
        "X-Auth-Email": email,
        "X-Auth-Key": api_key,
        "Content-Type": "application/json",
    }


async def _request(method: str, url: str, headers: dict, **kwargs) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method,
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
            **kwargs,
        ) as resp:
            data = await resp.json()
            if not data.get("success"):
                errors = data.get("errors", [])
                msg = (
                    "; ".join(e.get("message", str(e)) for e in errors)
                    if errors
                    else f"HTTP {resp.status}"
                )
                raise CloudflareAPIError(msg)
            return data


async def _get_zone_id(email: str, api_key: str, root_domain: str) -> str | None:
    data = await _request(
        "GET",
        f"{CF_BASE}/zones",
        _headers(email, api_key),
        params={"name": root_domain, "status": "active"},
    )
    zones = data.get("result", [])
    return zones[0]["id"] if zones else None


async def find_zone(email: str, api_key: str, full_domain: str) -> tuple[str, str] | None:
    """
    Finds zone_id by stripping leftmost labels of full_domain.
    Returns (zone_id, root_domain) or None if not found.
    """
    parts = full_domain.split(".")
    # Try from "skip 1 label" up to "only last 2 labels"
    for i in range(len(parts) - 1, 1, -1):
        candidate = ".".join(parts[-i:])
        zone_id = await _get_zone_id(email, api_key, candidate)
        if zone_id:
            return zone_id, candidate
    return None


async def get_a_record(email: str, api_key: str, zone_id: str, full_domain: str) -> dict | None:
    """Returns existing A record dict {id, content, ...} or None."""
    data = await _request(
        "GET",
        f"{CF_BASE}/zones/{zone_id}/dns_records",
        _headers(email, api_key),
        params={"type": "A", "name": full_domain},
    )
    records = data.get("result", [])
    return records[0] if records else None


async def create_a_record(email: str, api_key: str, zone_id: str, name: str, ip: str) -> str:
    """Creates A record, returns record_id."""
    data = await _request(
        "POST",
        f"{CF_BASE}/zones/{zone_id}/dns_records",
        _headers(email, api_key),
        json={"type": "A", "name": name, "content": ip, "ttl": 1, "proxied": False},
    )
    return data["result"]["id"]


async def update_a_record(
    email: str, api_key: str, zone_id: str, record_id: str, name: str, ip: str
) -> None:
    await _request(
        "PATCH",
        f"{CF_BASE}/zones/{zone_id}/dns_records/{record_id}",
        _headers(email, api_key),
        json={"type": "A", "name": name, "content": ip, "ttl": 1, "proxied": False},
    )


async def delete_a_record(email: str, api_key: str, zone_id: str, record_id: str) -> None:
    await _request(
        "DELETE",
        f"{CF_BASE}/zones/{zone_id}/dns_records/{record_id}",
        _headers(email, api_key),
    )
