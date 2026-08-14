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


async def get_sub_json(panel_url: str, api_token: str, short_uuid: str) -> list[dict]:
    """Fetch the Xray subscription JSON for a service user.

    Returns a list of full Xray configs (one per host).
    The endpoint is /api/sub/{short_uuid}/json.
    """
    data = await _get(f"{panel_url}/api/sub/{short_uuid}/json", api_token)
    return data if isinstance(data, list) else []


async def get_vless_config_for_tag(
    panel_url: str,
    api_token: str,
    service_short_uuid: str,
    host_tag: str,
) -> dict | None:
    """Build an Xray VLESS outbound config for a service user, filtered to host_tag.

    Flow:
    1. Fetch /api/sub/{service_short_uuid}/json — list of Xray configs (one per host).
    2. Fetch /api/hosts — get the domain (host field) for each tagged host.
    3. Match configs to tagged hosts via wsSettings.host == host.host.
    4. Return the first matching VLESS outbound (deep-copied).

    The returned dict's vnext[0].address must be replaced with the test IP
    before sending to Pingachock.  Returns None if no match found.
    """
    import copy

    # Step 1: get subscription configs for this service user
    try:
        configs = await get_sub_json(panel_url, api_token, service_short_uuid)
    except RemnaWaveAPIError as e:
        logger.warning("get_vless_config_for_tag: /api/sub/%s/json failed: %s", service_short_uuid, e)
        return None

    if not configs:
        logger.warning(
            "get_vless_config_for_tag: no configs returned for short_uuid=%s", service_short_uuid
        )
        return None

    # Step 2: collect domain names for hosts matching the tag
    try:
        all_hosts = await get_hosts(panel_url, api_token)
    except RemnaWaveAPIError as e:
        logger.warning("get_vless_config_for_tag: /api/hosts failed: %s", e)
        return None

    tagged_domains: set[str] = {
        h["host"] for h in all_hosts
        if host_tag in (h.get("tags") or []) and h.get("host")
    }

    if not tagged_domains:
        logger.warning(
            "get_vless_config_for_tag: no hosts with tag '%s' found (or none have 'host' domain set)",
            host_tag,
        )
        return None

    # Step 3: find the first outbound whose wsSettings.host matches a tagged domain
    for cfg in configs:
        for ob in cfg.get("outbounds", []):
            if ob.get("protocol") != "vless":
                continue
            ws_host = ob.get("streamSettings", {}).get("wsSettings", {}).get("host", "")
            if ws_host in tagged_domains:
                logger.debug(
                    "get_vless_config_for_tag: matched tag '%s' via host domain '%s'",
                    host_tag, ws_host,
                )
                return copy.deepcopy(ob)

    logger.warning(
        "get_vless_config_for_tag: no VLESS outbound found for tag '%s' "
        "(tagged domains: %s, configs: %d)",
        host_tag, tagged_domains, len(configs),
    )
    return None


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
