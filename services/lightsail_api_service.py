"""Async wrappers around the boto3 Amazon Lightsail API.

All boto3 calls are synchronous; we wrap them with asyncio.to_thread so
they run in the default thread pool without blocking the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import boto3
import botocore.exceptions

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

INSTANCE_PASSWORD = "FCC-L1ghts@il-2024!"   # set via cloud-init on every instance
PREFERRED_BLUEPRINTS = ["debian_13", "debian_12", "debian_11"]
CHEAPEST_BUNDLE = "nano_3_0"                 # 0.5 GB RAM, 1 vCPU, $3.50/mo

# cloud-init: enable SSH password auth + set root password
_USER_DATA = f"""#!/bin/bash
echo "root:{INSTANCE_PASSWORD}" | chpasswd
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
systemctl restart sshd 2>/dev/null || service sshd restart 2>/dev/null || true
"""

# All ports open (TCP + UDP full range + ICMP)
_OPEN_PORTS = [
    {"fromPort": 0, "toPort": 65535, "protocol": "tcp"},
    {"fromPort": 0, "toPort": 65535, "protocol": "udp"},
    {"fromPort": -1, "toPort": -1,    "protocol": "icmp"},
    {"fromPort": -1, "toPort": -1,    "protocol": "icmpv6"},
]


# ── client factory ────────────────────────────────────────────────────────────

def _make_client(region: str, access_key_id: str, secret_access_key: str):
    return boto3.client(
        "lightsail",
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    )


def _run(fn, *args, **kwargs):
    """Run a synchronous boto3 call in a thread pool."""
    return asyncio.to_thread(fn, *args, **kwargs)


# ── regions ───────────────────────────────────────────────────────────────────

async def get_regions(access_key_id: str, secret_access_key: str) -> list[dict]:
    """Return list of Lightsail-available regions with display names.

    Each dict: {"name": "us-east-1", "displayName": "N. Virginia"}.
    Uses us-east-1 as the control-plane region (regions API is global).
    """
    def _fetch():
        client = _make_client("us-east-1", access_key_id, secret_access_key)
        resp = client.get_regions(includeAvailabilityZones=False)
        return resp.get("regions", [])

    try:
        regions = await asyncio.to_thread(_fetch)
        return [{"name": r["name"], "displayName": r.get("displayName", r["name"])}
                for r in regions]
    except botocore.exceptions.ClientError as e:
        logger.error("Lightsail get_regions failed: %s", e)
        raise


# ── blueprints ────────────────────────────────────────────────────────────────

async def get_debian_blueprint(region: str, access_key_id: str, secret_access_key: str) -> str:
    """Return the blueprintId for the newest available Debian image."""
    def _fetch():
        client = _make_client(region, access_key_id, secret_access_key)
        resp = client.get_blueprints(includeInactive=False)
        return resp.get("blueprints", [])

    blueprints = await asyncio.to_thread(_fetch)
    bp_ids = {b["blueprintId"] for b in blueprints}
    for preferred in PREFERRED_BLUEPRINTS:
        if preferred in bp_ids:
            return preferred
    # Fallback: any Debian
    for bp in blueprints:
        if "debian" in bp.get("blueprintId", "").lower():
            return bp["blueprintId"]
    raise RuntimeError(f"No Debian blueprint available in region {region}")


# ── instances ─────────────────────────────────────────────────────────────────

async def create_instance(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    instance_name: str,
) -> None:
    """Create a Lightsail nano Debian instance with all ports open."""
    blueprint_id = await get_debian_blueprint(region, access_key_id, secret_access_key)

    def _create():
        client = _make_client(region, access_key_id, secret_access_key)
        client.create_instances(
            instanceNames=[instance_name],
            availabilityZone=f"{region}a",
            blueprintId=blueprint_id,
            bundleId=CHEAPEST_BUNDLE,
            userData=_USER_DATA,
        )

    await asyncio.to_thread(_create)
    logger.info("Lightsail: created instance %s in %s (blueprint=%s)", instance_name, region, blueprint_id)


async def get_instance_state(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    instance_name: str,
) -> str | None:
    """Return instance state string ('running', 'pending', 'stopped', …) or None if not found."""
    def _get():
        client = _make_client(region, access_key_id, secret_access_key)
        try:
            resp = client.get_instance(instanceName=instance_name)
            return resp["instance"]["state"]["name"]
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "NotFoundException":
                return None
            raise

    return await asyncio.to_thread(_get)


async def wait_for_instance_running(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    instance_name: str,
    timeout_seconds: int = 300,
    poll_interval: int = 10,
) -> bool:
    """Poll until instance state == 'running'. Returns False on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        state = await get_instance_state(region, access_key_id, secret_access_key, instance_name)
        if state == "running":
            return True
        if state is None:
            logger.warning("Instance %s not found while waiting", instance_name)
            return False
        logger.debug("Instance %s state=%s, waiting...", instance_name, state)
        await asyncio.sleep(poll_interval)
    logger.warning("Timeout waiting for instance %s to start", instance_name)
    return False


async def open_all_ports(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    instance_name: str,
) -> None:
    """Replace the firewall with a fully open rule set."""
    def _open():
        client = _make_client(region, access_key_id, secret_access_key)
        client.put_instance_public_ports(
            portInfos=_OPEN_PORTS,
            instanceName=instance_name,
        )

    await asyncio.to_thread(_open)
    logger.info("Lightsail: opened all ports on %s", instance_name)


async def delete_instance(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    instance_name: str,
) -> None:
    """Delete an instance (best-effort; ignores NotFoundException)."""
    def _delete():
        client = _make_client(region, access_key_id, secret_access_key)
        try:
            client.delete_instance(instanceName=instance_name)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] != "NotFoundException":
                raise

    await asyncio.to_thread(_delete)
    logger.info("Lightsail: deleted instance %s", instance_name)


async def instance_exists(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    instance_name: str,
) -> bool:
    state = await get_instance_state(region, access_key_id, secret_access_key, instance_name)
    return state is not None


# ── static IPs ────────────────────────────────────────────────────────────────

def _short_id() -> str:
    return uuid.uuid4().hex[:6]


async def allocate_static_ip(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    name: str,
) -> dict:
    """Allocate a new static IP. Returns {"name": ..., "ipAddress": ...}."""
    def _alloc():
        client = _make_client(region, access_key_id, secret_access_key)
        client.allocate_static_ip(staticIpName=name)
        resp = client.get_static_ip(staticIpName=name)
        ip = resp["staticIp"]
        return {"name": ip["name"], "ipAddress": ip["ipAddress"]}

    result = await asyncio.to_thread(_alloc)
    logger.info("Lightsail: allocated static IP %s = %s", result["name"], result["ipAddress"])
    return result


async def attach_static_ip(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    static_ip_name: str,
    instance_name: str,
) -> None:
    def _attach():
        client = _make_client(region, access_key_id, secret_access_key)
        client.attach_static_ip(
            staticIpName=static_ip_name,
            instanceName=instance_name,
        )

    await asyncio.to_thread(_attach)
    logger.info("Lightsail: attached %s to %s", static_ip_name, instance_name)


async def detach_static_ip(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    static_ip_name: str,
) -> None:
    """Detach (but don't release) a static IP. Ignores if already detached."""
    def _detach():
        client = _make_client(region, access_key_id, secret_access_key)
        try:
            client.detach_static_ip(staticIpName=static_ip_name)
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code not in ("NotFoundException", "InvalidInputException"):
                raise

    await asyncio.to_thread(_detach)
    logger.info("Lightsail: detached %s", static_ip_name)


async def release_static_ip(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    static_ip_name: str,
) -> None:
    """Release (free) a static IP. Ignores NotFoundException."""
    def _release():
        client = _make_client(region, access_key_id, secret_access_key)
        try:
            client.release_static_ip(staticIpName=static_ip_name)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] != "NotFoundException":
                raise

    await asyncio.to_thread(_release)
    logger.info("Lightsail: released %s", static_ip_name)


async def get_static_ip_address(
    region: str,
    access_key_id: str,
    secret_access_key: str,
    static_ip_name: str,
) -> str | None:
    """Return the IP address of a named static IP, or None if not found."""
    def _get():
        client = _make_client(region, access_key_id, secret_access_key)
        try:
            resp = client.get_static_ip(staticIpName=static_ip_name)
            return resp["staticIp"]["ipAddress"]
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "NotFoundException":
                return None
            raise

    return await asyncio.to_thread(_get)
