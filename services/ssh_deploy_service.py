from __future__ import annotations
import asyncio
import base64
import shlex
from typing import Awaitable, Callable

import asyncssh

ProgressCallback = Callable[[str], Awaitable[None]]


class SSHKeyPassphraseRequired(Exception):
    pass


class SSHAuthError(Exception):
    pass


def parse_private_key(data: bytes, passphrase: str | None = None) -> asyncssh.SSHKey:
    try:
        return asyncssh.import_private_key(data, passphrase=passphrase)
    except asyncssh.KeyImportError as e:
        if passphrase is None and "passphrase" in str(e).lower():
            raise SSHKeyPassphraseRequired() from e
        raise SSHAuthError(f"Не удалось прочитать ключ: {e}") from e
    except asyncssh.KeyEncryptionError as e:
        raise SSHAuthError(f"Неверный key-phrase: {e}") from e


_COMPOSE_TEMPLATE = """\
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
"""


def _build_deploy_script(secret_key: str, port: int) -> str:
    compose = _COMPOSE_TEMPLATE.format(port=port, secret_key=secret_key)
    # base64-encode the compose file to safely pass it through the shell
    compose_b64 = base64.b64encode(compose.encode()).decode()
    return "\n".join([
        "set -e",
        # Install Docker if not present
        "command -v docker >/dev/null 2>&1 || (apt-get update -y && curl -fsSL https://get.docker.com | sh)",
        # Prepare directory
        "mkdir -p /opt/remnanode",
        # Write compose file via base64 — avoids all quoting/escaping issues
        f"printf '%s' '{compose_b64}' | base64 -d > /opt/remnanode/docker-compose.yml",
        # Pull image and start
        "cd /opt/remnanode && docker compose pull && docker compose up -d",
    ])


async def deploy_remnanode(
    *,
    ip: str,
    login: str,
    password: str | None,
    client_key: asyncssh.SSHKey | None,
    secret_key: str,
    port: int,
    progress_cb: ProgressCallback,
) -> str:
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
        await progress_cb("🔌 Подключено. Запускаю установку...")

        script = _build_deploy_script(secret_key, port)
        sudo_cmd = f"sudo -S -p '' bash -c {shlex.quote(script)}"

        start = asyncio.get_running_loop().time()

        async def _ticker() -> None:
            while True:
                await asyncio.sleep(15)
                elapsed = int(asyncio.get_running_loop().time() - start)
                m, s = divmod(elapsed, 60)
                await progress_cb(f"⚙️ Устанавливаю Remnawave Node... {m}м {s}с")

        ticker = asyncio.create_task(_ticker())
        try:
            result = await conn.run(
                sudo_cmd,
                input=(password + "\n") if password else None,
                check=True,
                timeout=1800,
            )
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass

        return result.stdout or ""
