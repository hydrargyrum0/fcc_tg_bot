from __future__ import annotations
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

import asyncssh

ProgressCallback = Callable[[str], Awaitable[None]]

PLAYBOOKS_DIR = Path(__file__).parent.parent / "ansible" / "playbooks"

_TASK_RE = re.compile(r"^TASK \[(.+?)\]")
_STATUS_RE = re.compile(r"^(ok|changed|skipping|failed|fatal):")

_EMOJI = {
    "ok": "✅",
    "changed": "✅",
    "skipping": "⏭",
    "failed": "❌",
    "fatal": "❌",
}


def _fmt(last: tuple[str, str] | None, current: str | None) -> str:
    parts = []
    if last:
        parts.append(f"{last[0]} {last[1]}")
    if current:
        parts.append(f"⏳ {current}...")
    return " | ".join(parts) if parts else "🔌 Подключаюсь..."


async def run_playbook(
    playbook: str,
    host: str,
    login: str,
    password: str | None = None,
    key_data: bytes | None = None,
    key_passphrase: str | None = None,
    become_pass: str | None = None,
    extra_vars: dict | None = None,
    progress_cb: ProgressCallback | None = None,
) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            inv_path = _write_inventory(tmpdir, host, login, password, key_data, key_passphrase, become_pass)
        except Exception as e:
            return False, f"Ошибка подготовки SSH-ключа: {e}"

        cmd = _build_cmd(playbook, inv_path, extra_vars)
        env = _build_env()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except FileNotFoundError:
            return False, "ansible-playbook не найден (установите ansible-core)"

        if progress_cb:
            await progress_cb("🔌 Подключаюсь...")

        last_completed: tuple[str, str] | None = None
        current_task = ""
        current_status = ""
        error_lines: list[str] = []

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()

            m = _TASK_RE.match(line)
            if m:
                task_name = m.group(1).strip()
                # Finalize the previous task before switching
                if current_task:
                    emoji = _EMOJI.get(current_status, "✅")
                    last_completed = (emoji, current_task)
                if task_name != "Gathering Facts":
                    current_task = task_name
                    current_status = ""
                    if progress_cb:
                        await progress_cb(_fmt(last_completed, current_task))
                continue

            m = _STATUS_RE.match(line)
            if m and current_task:
                current_status = m.group(1)
                if current_status in ("failed", "fatal"):
                    error_lines.append(line[:300])
                continue

            # Ansible marks ignored failures with this string
            if "...ignoring" in line and current_status in ("failed", "fatal"):
                current_status = "ok"

        # Finalize the last task
        if current_task:
            emoji = _EMOJI.get(current_status, "✅")
            last_completed = (emoji, current_task)

        await proc.wait()
        success = proc.returncode == 0

        if progress_cb:
            if success:
                await progress_cb(_fmt(last_completed, None))
            else:
                snippet = error_lines[-1][:120] if error_lines else "Неизвестная ошибка"
                await progress_cb(f"❌ {snippet}")

        return success, "\n".join(error_lines)


def _write_inventory(
    tmpdir: str,
    host: str,
    login: str,
    password: str | None,
    key_data: bytes | None,
    key_passphrase: str | None,
    become_pass: str | None,
) -> str:
    ssh_extra = (
        "-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null "
        "-o ServerAliveInterval=10 "
        "-o ServerAliveCountMax=60"
    )
    opts = [
        f"ansible_user={login}",
        f"ansible_ssh_extra_args='{ssh_extra}'",
    ]

    if key_data:
        key = asyncssh.import_private_key(key_data, passphrase=key_passphrase)
        exported: bytes = key.export_private_key()
        if isinstance(exported, str):
            exported = exported.encode()
        key_path = os.path.join(tmpdir, "id_rsa")
        with open(key_path, "wb") as fh:
            fh.write(exported)
        os.chmod(key_path, 0o600)
        opts.append(f"ansible_private_key_file={key_path}")
    elif password:
        opts.append(f"ansible_password={password}")

    if become_pass:
        opts.append(f"ansible_become_pass={become_pass}")

    inv_path = os.path.join(tmpdir, "inventory.ini")
    with open(inv_path, "w") as fh:
        fh.write(f"[target]\n{host} {' '.join(opts)}\n")
    return inv_path


def _build_cmd(playbook: str, inv_path: str, extra_vars: dict | None) -> list[str]:
    ansible_bin = str(Path(sys.executable).parent / "ansible-playbook")
    playbook_path = str(PLAYBOOKS_DIR / f"{playbook}.yml")
    cmd = [ansible_bin, playbook_path, "-i", inv_path]
    if extra_vars:
        cmd += ["--extra-vars", json.dumps(extra_vars)]
    return cmd


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ANSIBLE_FORCE_COLOR"] = "0"
    env["ANSIBLE_STDOUT_CALLBACK"] = "default"
    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    env["ANSIBLE_TIMEOUT"] = "30"
    env["ANSIBLE_PERSISTENT_COMMAND_TIMEOUT"] = "600"
    return env
