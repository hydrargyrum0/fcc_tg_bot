import aiohttp


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
    """Updates the address field of a single host."""
    await _patch(f"{panel_url}/api/hosts/{host_uuid}", api_token, {"address": address})


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
