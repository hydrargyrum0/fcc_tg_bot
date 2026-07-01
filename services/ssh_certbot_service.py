import asyncio
import base64
import shlex
from typing import Awaitable, Callable

import asyncssh

ProgressCallback = Callable[[str], Awaitable[None]]

_CERTBOT_COMPOSE = """\
services:
  certbot:
    container_name: certbot
    image: certbot/certbot
    network_mode: host
    volumes:
      - ./certs:/etc/letsencrypt
"""

_REMNANODE_COMPOSE_WITH_CERTS = """\
services:
  remnanode:
    container_name: remnanode
    hostname: remnanode
    image: remnawave/node:latest
    network_mode: host
    restart: always
    cap_add:
      - NET_ADMIN
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    environment:
      - NODE_PORT={port}
      - SECRET_KEY={secret_key}
    volumes:
      - /opt/certbot/certs:/etc/letsencrypt:ro
"""


def _build_certbot_script(
    domain: str, letsencrypt_email: str, node_secret: str, node_port: int
) -> str:
    certbot_compose_b64 = base64.b64encode(_CERTBOT_COMPOSE.encode()).decode()
    remnanode_compose = _REMNANODE_COMPOSE_WITH_CERTS.format(
        port=node_port,
        secret_key=node_secret,
    )
    remnanode_compose_b64 = base64.b64encode(remnanode_compose.encode()).decode()

    domain_q = shlex.quote(domain)
    email_q = shlex.quote(letsencrypt_email)

    certbot_cmd = (
        f"docker run --rm"
        f" -v /opt/certbot/certs:/etc/letsencrypt"
        f" -v /opt/certbot/var-lib-letsencrypt:/var/lib/letsencrypt"
        f" --network host"
        f" certbot/certbot certonly --standalone"
        f" --non-interactive --agree-tos"
        f" --email {email_q}"
        f" -d {domain_q}"
    )

    return "\n".join([
        "set -e",
        "command -v docker >/dev/null 2>&1 || (apt-get update -y && curl -fsSL https://get.docker.com | sh)",
        # Certbot setup
        "mkdir -p /opt/certbot/certs /opt/certbot/var-lib-letsencrypt",
        f"printf '%s' '{certbot_compose_b64}' | base64 -d > /opt/certbot/docker-compose.yml",
        # Get certificate (standalone mode — port 80 must be free)
        certbot_cmd,
        # Update remnanode docker-compose to mount the certs volume
        f"printf '%s' '{remnanode_compose_b64}' | base64 -d > /opt/remnanode/docker-compose.yml",
        # Restart remnanode with the new compose
        "cd /opt/remnanode && docker compose down && docker compose up -d",
    ])


async def setup_domain_cert(
    *,
    ip: str,
    login: str,
    password: str | None,
    client_key: asyncssh.SSHKey | None,
    domain: str,
    letsencrypt_email: str,
    node_secret: str,
    node_port: int,
    progress_cb: ProgressCallback,
) -> None:
    connect_kwargs: dict = {
        "host": ip,
        "username": login,
        "known_hosts": None,
        "connect_timeout": 20,
    }
    if client_key is not None:
        connect_kwargs["client_keys"] = [client_key]
    if password is not None:
        connect_kwargs["password"] = password

    async with asyncssh.connect(**connect_kwargs) as conn:
        await progress_cb("🔌 Подключено. Получаю SSL-сертификат...")

        script = _build_certbot_script(domain, letsencrypt_email, node_secret, node_port)
        sudo_cmd = f"sudo -S -p '' bash -c {shlex.quote(script)}"

        start = asyncio.get_event_loop().time()

        async def _ticker() -> None:
            while True:
                await asyncio.sleep(15)
                elapsed = int(asyncio.get_event_loop().time() - start)
                m, s = divmod(elapsed, 60)
                await progress_cb(f"⚙️ Получаю сертификат... {m}м {s}с")

        ticker = asyncio.create_task(_ticker())
        try:
            await conn.run(
                sudo_cmd,
                input=(password + "\n") if password else None,
                check=True,
                timeout=600,
            )
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
