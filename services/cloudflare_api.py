from __future__ import annotations
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
    # Try input itself first, then strip leftmost labels until 2 remain
    for i in range(len(parts), 1, -1):
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


async def get_zone(email: str, api_key: str, zone_id: str) -> dict | None:
    """Returns zone details {id, name, ...} or None."""
    data = await _request(
        "GET",
        f"{CF_BASE}/zones/{zone_id}",
        _headers(email, api_key),
    )
    return data.get("result")


async def list_zones(email: str, api_key: str) -> list[dict]:
    """Returns all active zones in the account sorted by name."""
    all_zones: list[dict] = []
    page = 1
    while True:
        data = await _request(
            "GET",
            f"{CF_BASE}/zones",
            _headers(email, api_key),
            params={"per_page": 50, "status": "active", "page": page},
        )
        result = data.get("result", [])
        all_zones.extend(result)
        info = data.get("result_info", {})
        if len(all_zones) >= info.get("total_count", len(all_zones)):
            break
        page += 1
    return sorted(all_zones, key=lambda z: z["name"])


async def list_dns_records(email: str, api_key: str, zone_id: str) -> list[dict]:
    """Returns all DNS records for a zone sorted by type then name."""
    all_records: list[dict] = []
    page = 1
    while True:
        data = await _request(
            "GET",
            f"{CF_BASE}/zones/{zone_id}/dns_records",
            _headers(email, api_key),
            params={"per_page": 100, "page": page},
        )
        result = data.get("result", [])
        all_records.extend(result)
        info = data.get("result_info", {})
        if len(all_records) >= info.get("total_count", len(all_records)):
            break
        page += 1
    return sorted(all_records, key=lambda r: (r["type"], r["name"]))
