import logging

import aiohttp

logger = logging.getLogger(__name__)


class RemnaWaveAPIError(Exception):
    pass


async def _get(url: str, api_token: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise RemnaWaveAPIError(f"HTTP {resp.status}: {text[:200]}")
            return await resp.json()


async def _patch(url: str, api_token: str, payload: dict) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.patch(
            url,
            headers={"Authorization": f"Bearer {api_token}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise RemnaWaveAPIError(f"HTTP {resp.status}: {text[:200]}")
            return await resp.json()


async def _post(url: str, api_token: str, payload: dict) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={"Authorization": f"Bearer {api_token}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if not resp.ok:
                text = await resp.text()
                raise RemnaWaveAPIError(f"HTTP {resp.status}: {text[:200]}")
            return await resp.json()


async def check_panel_online(panel_url: str, api_token: str) -> bool:
    try:
        await _get(f"{panel_url}/api/nodes/tags", api_token)
        return True
    except Exception:
        return False


async def get_nodes(panel_url: str, api_token: str) -> list[dict]:
    """Returns list of node dicts with isConnected, isDisabled, etc."""
    data = await _get(f"{panel_url}/api/nodes", api_token)
    return data["response"]


async def restart_node(panel_url: str, api_token: str, node_uuid: str, force_restart: bool) -> bool:
    data = await _post(
        f"{panel_url}/api/nodes/{node_uuid}/actions/restart",
        api_token,
        {"forceRestart": force_restart},
    )
    return data["response"]["eventSent"]


async def enable_node(panel_url: str, api_token: str, node_uuid: str) -> None:
    await _post(f"{panel_url}/api/nodes/{node_uuid}/actions/enable", api_token, {})


async def disable_node(panel_url: str, api_token: str, node_uuid: str) -> None:
    await _post(f"{panel_url}/api/nodes/{node_uuid}/actions/disable", api_token, {})


async def get_config_profiles(panel_url: str, api_token: str) -> list[dict]:
    """Returns list of {uuid, name, ...} dicts."""
    data = await _get(f"{panel_url}/api/config-profiles", api_token)
    return data["response"]["configProfiles"]


async def get_profile_inbounds(panel_url: str, api_token: str, profile_uuid: str) -> list[str]:
    """Returns list of inbound UUIDs for the given profile."""
    data = await _get(f"{panel_url}/api/config-profiles/{profile_uuid}/inbounds", api_token)
    return [inbound["uuid"] for inbound in data["response"]["inbounds"]]


async def get_config_profile(panel_url: str, api_token: str, profile_uuid: str) -> dict:
    """Returns full profile dict including config (XRay JSON) and inbounds list."""
    data = await _get(f"{panel_url}/api/config-profiles/{profile_uuid}", api_token)
    return data["response"]


async def get_profile_inbounds_detailed(panel_url: str, api_token: str, profile_uuid: str) -> list[dict]:
    """Returns inbounds with type, network, security, port, etc."""
    data = await _get(f"{panel_url}/api/config-profiles/{profile_uuid}/inbounds", api_token)
    return data["response"]["inbounds"]


async def update_config_profile(panel_url: str, api_token: str, profile_uuid: str, config: dict) -> None:
    """Replaces the XRay config JSON of a config profile."""
    await _patch(
        f"{panel_url}/api/config-profiles",
        api_token,
        {"uuid": profile_uuid, "config": config},
    )


async def get_hosts(panel_url: str, api_token: str) -> list[dict]:
    """Returns list of host dicts with uuid, remark, address, tags, etc."""
    data = await _get(f"{panel_url}/api/hosts", api_token)
    return data["response"]


async def update_host_address(panel_url: str, api_token: str, host_uuid: str, address: str) -> None:
    """Updates the address field of a single host. uuid goes in body, not URL."""
    await _patch(f"{panel_url}/api/hosts", api_token, {"uuid": host_uuid, "address": address})


async def get_users(panel_url: str, api_token: str) -> list[dict]:
    """Fetch all users from Remnawave panel."""
    data = await _get(f"{panel_url}/api/users", api_token)
    # Remnawave wraps in {"response": {"users": [...], ...}}
    response = data.get("response", data)
    if isinstance(response, list):
        return response
    return response.get("users", [])


async def get_vless_config_for_tag(
    panel_url: str,
    api_token: str,
    service_tg_id: int,
    host_tag: str,
) -> dict | None:
    """Build an Xray VLESS outbound config for a service user filtered to host_tag.

    Returns an Xray-compatible outbound dict ready to send to Pingachock.
    The `address` field is a placeholder — substitute the test IP before sending.
    Returns None if the service user or tagged host is not found.

    NOTE: The exact field names depend on your Remnawave version.
    If this returns None unexpectedly, check the raw /api/users response
    and adjust field names (telegramId, uuid, subCredentials, etc.) below.
    """
    # Step 1: find service user by Telegram ID
    users = await get_users(panel_url, api_token)
    if not users:
        logger.warning("get_vless_config_for_tag: /api/users returned empty list")
        return None
    # Log first user's keys to help diagnose field name issues
    logger.warning(
        "get_vless_config_for_tag: %d users found, first user keys: %s, sample telegramId field: %r",
        len(users),
        list(users[0].keys()),
        users[0].get("telegramId"),
    )
    # Match by int or string to handle API variations
    service_user = next(
        (u for u in users
         if u.get("telegramId") == service_tg_id
         or str(u.get("telegramId", "")) == str(service_tg_id)),
        None,
    )
    if not service_user:
        # Log all telegramId values to help find the right user
        tg_ids = [(u.get("telegramId"), u.get("username") or u.get("name") or u.get("email", "?")) for u in users[:10]]
        logger.warning(
            "get_vless_config_for_tag: service user tg_id=%d not found. First 10 users (tg_id, name): %s",
            service_tg_id, tg_ids,
        )
        return None

    # Step 2: find a host matching the tag
    hosts = await get_hosts(panel_url, api_token)
    tagged = [h for h in hosts if host_tag in (h.get("tags") or [])]
    if not tagged:
        return None
    host = tagged[0]  # use first matching host as config template

    # Step 3: extract user UUID
    # Remnawave may store it as user["uuid"] or user["subCredentials"][0]["uuid"]
    user_uuid: str = service_user.get("uuid") or ""
    if not user_uuid:
        creds = service_user.get("subCredentials") or []
        if creds:
            user_uuid = creds[0].get("uuid", "")

    # Step 4: extract host connection parameters
    address = str(host.get("address") or "").split(":")[0]  # placeholder; caller replaces
    port = int(host.get("port") or 443)
    network = str(host.get("network") or "tcp")
    security = str(host.get("security") or "none")

    outbound: dict = {
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": address,   # REPLACE WITH TEST IP before Pingachock submission
                "port": port,
                "users": [{"id": user_uuid, "encryption": "none"}],
            }]
        },
        "streamSettings": {
            "network": network,
            "security": security,
        },
    }

    # Carry through transport-specific settings when present in host config
    if host.get("tls_settings"):
        outbound["streamSettings"]["tlsSettings"] = host["tls_settings"]
    if host.get("reality_settings"):
        outbound["streamSettings"]["realitySettings"] = host["reality_settings"]
    if network == "ws" or host.get("ws_settings"):
        ws_path = host.get("path") or "/"
        ws_host = host.get("host") or ""
        outbound["streamSettings"]["wsSettings"] = {
            "path": ws_path,
            "headers": {"Host": ws_host} if ws_host else {},
        }
    if network == "grpc" or host.get("grpc_settings"):
        outbound["streamSettings"]["grpcSettings"] = host.get("grpc_settings") or {}

    return outbound


async def create_node(
    panel_url: str,
    api_token: str,
    name: str,
    address: str,
    port: int,
    profile_uuid: str,
    inbound_uuids: list[str],
) -> dict:
    """Creates node, returns response dict with uuid and name."""
    data = await _post(
        f"{panel_url}/api/nodes",
        api_token,
        {
            "name": name,
            "address": address,
            "port": port,
            "configProfile": {
                "activeConfigProfileUuid": profile_uuid,
                "activeInbounds": inbound_uuids,
            },
        },
    )
    return data["response"]
