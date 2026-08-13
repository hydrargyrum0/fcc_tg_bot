# Managed IP Pool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a managed/scored IP pool per automation group: background scoring via Pingachock (ping + TLS + VLESS speedtest), with the availability monitor using pool IPs for replacements instead of real-time raw-set scanning.

**Architecture:** New entities `ManagedPool` + `ManagedIp`; background scorer `ip_pool_scorer.py`; automation groups gain optional `managed_pool_id`; availability monitor queries scored pool instead of doing live Pingachock search. UI: `menu:ip_sets` splits into "Пользовательские списки" / "Модерируемые пулы"; automation FSM adds source-type step.

**Tech Stack:** Python 3.12, aiogram v3, SQLAlchemy async, PostgreSQL, asyncio, Pingachock API v1, Alembic migrations.

---

### Task 1: normalize_addresses() — smart IP extraction from any text

**Files:**
- Modify: `services/ip_check_service.py`
- Modify: `bot/handlers/ip_sets.py`

**Why:** Current `_parse_addresses` in `ip_sets.py` rejects any line that isn't a bare IP or CIDR. New `normalize_addresses` uses regex to extract valid IPs/CIDRs from arbitrary text (JSON, markdown, comments, etc.), then expands CIDRs. This handles real-world pool files with garbage metadata.

- [ ] **Step 1: Add normalize_addresses to ip_check_service.py**

Add after the `expand_addresses` function:

```python
import re as _re

# Matches bare IPv4, IPv4/CIDR, bare IPv6, IPv6/CIDR
_IP_PATTERN = _re.compile(
    r'\b(?:'
    r'(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?'           # IPv4 (with optional CIDR)
    r'|(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?:/\d{1,3})?'  # IPv6
    r')\b'
)


def normalize_addresses(text: str, cap: int | None = None) -> tuple[list[str], int]:
    """Extract and expand all IP/CIDR entries from arbitrary text.

    Ignores surrounding garbage (comments, JSON keys, etc.).
    Returns (ips, skipped_count) where skipped_count is invalid matches dropped.
    """
    raw_matches = _IP_PATTERN.findall(text)
    ips: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for entry in raw_matches:
        try:
            net = ipaddress.ip_network(entry, strict=False)
            if net.num_addresses == 1:
                ip = str(net.network_address)
                if ip not in seen:
                    seen.add(ip)
                    ips.append(ip)
                    if cap and len(ips) >= cap:
                        return ips, skipped
            else:
                for host in net.hosts():
                    ip = str(host)
                    if ip not in seen:
                        seen.add(ip)
                        ips.append(ip)
                        if cap and len(ips) >= cap:
                            return ips, skipped
        except ValueError:
            skipped += 1
    return ips, skipped
```

- [ ] **Step 2: Update ip_sets.py to use normalize_addresses**

Replace the `_parse_addresses` function and update `_process_addresses`:

```python
# At the top, add import:
from services.ip_check_service import normalize_addresses

# Remove _parse_addresses function entirely.
# Replace _process_addresses:

async def _process_addresses(message: Message, state: FSMContext, raw: str) -> None:
    valid, skipped = normalize_addresses(raw, cap=_MAX_ENTRIES + 1)

    if not valid:
        await message.answer(
            "❌ Не найдено ни одного корректного IP-адреса в тексте.\n\n"
            "Отправьте список адресов или файл с адресами:",
            reply_markup=ip_set_cancel_kb(),
        )
        return

    if len(valid) > _MAX_ENTRIES:
        await message.answer(
            f"❌ Слишком много записей ({len(valid):,}). Максимум {_MAX_ENTRIES:,}.",
            reply_markup=ip_set_cancel_kb(),
        )
        return

    addresses_text = "\n".join(valid)
    await state.update_data(addresses=addresses_text)
    await state.set_state(AddIpSet.waiting_tag)

    warn = f"\n⚠️ Пропущено нераспознанных фрагментов: {skipped}" if skipped else ""
    await message.answer(
        f"✅ Принято {len(valid):,} адресов.{warn}\n\n"
        "Введите тег для этого набора (например: <code>RU-subnets</code>):",
        reply_markup=ip_set_cancel_kb(),
        parse_mode="HTML",
    )
```

Also update the prompt text in `ipset_add_start` and `ipset_cancel` to reflect that any format is accepted:
```python
"➕ <b>Новый набор IP</b>\n\n"
"Отправьте список адресов в любом формате — извлечём все IP автоматически.\n"
"Поддерживаются одиночные IP, подсети CIDR, любой сопутствующий текст.\n\n"
"Или прикрепите <b>.txt файл</b>.",
```

- [ ] **Step 3: Syntax check and commit**

```bash
python -c "import ast, pathlib; [ast.parse(pathlib.Path(f).read_text(encoding='utf-8')) for f in ['services/ip_check_service.py','bot/handlers/ip_sets.py']]; print('OK')"
git add services/ip_check_service.py bot/handlers/ip_sets.py
git commit -m "feat: normalize_addresses — extract IPs from arbitrary text in ip sets"
```

---

### Task 2: DB models — ManagedPool, ManagedIp, AutomationGroup update

**Files:**
- Create: `db/models/managed_pool.py`
- Modify: `db/models/automation_group.py`

- [ ] **Step 1: Create db/models/managed_pool.py**

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class ManagedPool(Base):
    __tablename__ = "managed_pools"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    host_tag: Mapped[str] = mapped_column(String(200), nullable=False)
    ip_set_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    score_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=60.0)
    check_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ManagedIp(Base):
    __tablename__ = "managed_ips"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("managed_pools.id", ondelete="CASCADE"), nullable=False
    )
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    ping_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ping_loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tls_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tls_handshake_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    vless_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    vless_speed_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

- [ ] **Step 2: Add managed_pool_id to AutomationGroup**

In `db/models/automation_group.py`, add after the `created_at` column:

```python
managed_pool_id: Mapped[int | None] = mapped_column(
    ForeignKey("managed_pools.id", ondelete="SET NULL"), nullable=True
)
```

Also add at the top: `from sqlalchemy import ..., BigInteger` (or keep Integer) — already imported.

- [ ] **Step 3: Syntax check**

```bash
python -c "import ast, pathlib; [ast.parse(pathlib.Path(f).read_text(encoding='utf-8')) for f in ['db/models/managed_pool.py','db/models/automation_group.py']]; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add db/models/managed_pool.py db/models/automation_group.py
git commit -m "feat: add ManagedPool, ManagedIp models; add managed_pool_id to AutomationGroup"
```

---

### Task 3: Alembic migration

**Files:**
- Create: `db/migrations/versions/c4d5e6f7a8b9_add_managed_pools.py`

- [ ] **Step 1: Create migration file**

```python
"""add_managed_pools

Revision ID: c4d5e6f7a8b9
Revises: a2b3c4d5e6f7
Create Date: 2026-08-14 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'managed_pools',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('host_tag', sa.String(length=200), nullable=False),
        sa.Column('ip_set_ids', sa.JSON(), nullable=False),
        sa.Column('score_threshold', sa.Float(), nullable=False, server_default='60.0'),
        sa.Column('check_interval_minutes', sa.Integer(), nullable=False, server_default='120'),
        sa.Column('last_scanned_at', sa.DateTime(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'managed_ips',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('pool_id', sa.Integer(), nullable=False),
        sa.Column('ip', sa.String(length=45), nullable=False),
        sa.Column('score', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('is_approved', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('ping_rtt_ms', sa.Float(), nullable=True),
        sa.Column('ping_loss_pct', sa.Float(), nullable=True),
        sa.Column('tls_ok', sa.Boolean(), nullable=True),
        sa.Column('tls_handshake_ms', sa.Float(), nullable=True),
        sa.Column('vless_ok', sa.Boolean(), nullable=True),
        sa.Column('vless_speed_mbps', sa.Float(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['pool_id'], ['managed_pools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pool_id', 'ip', name='uq_managed_ips_pool_ip'),
    )

    op.add_column(
        'automation_groups',
        sa.Column('managed_pool_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_automation_groups_managed_pool_id',
        'automation_groups', 'managed_pools',
        ['managed_pool_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_automation_groups_managed_pool_id', 'automation_groups', type_='foreignkey')
    op.drop_column('automation_groups', 'managed_pool_id')
    op.drop_table('managed_ips')
    op.drop_table('managed_pools')
```

- [ ] **Step 2: Apply migration**

```bash
docker compose exec bot alembic upgrade head
```

Expected output: `Running upgrade a2b3c4d5e6f7 -> c4d5e6f7a8b9, add_managed_pools`

- [ ] **Step 3: Commit**

```bash
git add db/migrations/versions/c4d5e6f7a8b9_add_managed_pools.py
git commit -m "feat: migration — add managed_pools, managed_ips, managed_pool_id on automation_groups"
```

---

### Task 4: ManagedPoolService — CRUD

**Files:**
- Create: `services/managed_pool_service.py`

- [ ] **Step 1: Create services/managed_pool_service.py**

```python
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.managed_pool import ManagedIp, ManagedPool


class ManagedPoolService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # ── Pool CRUD ──────────────────────────────────────────────────────────────

    async def get_org_pools(self, org_id: int) -> list[ManagedPool]:
        result = await self._s.execute(
            select(ManagedPool)
            .where(ManagedPool.org_id == org_id)
            .order_by(ManagedPool.created_at)
        )
        return list(result.scalars().all())

    async def get_pool(self, pool_id: int, org_id: int) -> ManagedPool | None:
        result = await self._s.execute(
            select(ManagedPool).where(
                ManagedPool.id == pool_id,
                ManagedPool.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_pool_any(self, pool_id: int) -> ManagedPool | None:
        """For background tasks — no org check."""
        result = await self._s.execute(
            select(ManagedPool).where(ManagedPool.id == pool_id)
        )
        return result.scalar_one_or_none()

    async def get_all_enabled(self) -> list[ManagedPool]:
        result = await self._s.execute(
            select(ManagedPool).where(ManagedPool.enabled == True)  # noqa: E712
        )
        return list(result.scalars().all())

    async def create_pool(
        self,
        org_id: int,
        name: str,
        host_tag: str,
        ip_set_ids: list[int],
        score_threshold: float = 60.0,
        check_interval_minutes: int = 120,
    ) -> ManagedPool:
        pool = ManagedPool(
            org_id=org_id,
            name=name,
            host_tag=host_tag,
            ip_set_ids=ip_set_ids,
            score_threshold=score_threshold,
            check_interval_minutes=check_interval_minutes,
        )
        self._s.add(pool)
        await self._s.commit()
        await self._s.refresh(pool)
        return pool

    async def delete_pool(self, pool_id: int, org_id: int) -> bool:
        pool = await self.get_pool(pool_id, org_id)
        if not pool:
            return False
        await self._s.execute(
            delete(ManagedPool).where(ManagedPool.id == pool_id)
        )
        await self._s.commit()
        return True

    async def mark_scanned(self, pool_id: int) -> None:
        await self._s.execute(
            update(ManagedPool)
            .where(ManagedPool.id == pool_id)
            .values(last_scanned_at=datetime.now(timezone.utc))
        )
        await self._s.commit()

    # ── ManagedIp CRUD ─────────────────────────────────────────────────────────

    async def get_pool_stats(self, pool_id: int) -> dict:
        """Returns {total, approved, pending, rejected}."""
        result = await self._s.execute(
            select(ManagedIp).where(ManagedIp.pool_id == pool_id)
        )
        all_ips = list(result.scalars().all())
        total = len(all_ips)
        approved = sum(1 for ip in all_ips if ip.is_approved)
        pending = sum(1 for ip in all_ips if ip.last_checked_at is None)
        rejected = total - approved - pending
        return {"total": total, "approved": approved, "pending": pending, "rejected": rejected}

    async def get_approved_ips(self, pool_id: int) -> list[ManagedIp]:
        """Approved IPs sorted by score descending — used by availability monitor."""
        result = await self._s.execute(
            select(ManagedIp)
            .where(ManagedIp.pool_id == pool_id, ManagedIp.is_approved == True)  # noqa
            .order_by(ManagedIp.score.desc())
        )
        return list(result.scalars().all())

    async def get_ips_to_check(self, pool_id: int, stale_after_minutes: int) -> list[ManagedIp]:
        """IPs that have never been checked or whose last_checked_at is stale."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
        result = await self._s.execute(
            select(ManagedIp).where(
                ManagedIp.pool_id == pool_id,
                (ManagedIp.last_checked_at == None) |  # noqa
                (ManagedIp.last_checked_at < cutoff),
            )
        )
        return list(result.scalars().all())

    async def upsert_ip(self, pool_id: int, ip: str) -> ManagedIp:
        """Insert if not exists, return existing otherwise."""
        result = await self._s.execute(
            select(ManagedIp).where(ManagedIp.pool_id == pool_id, ManagedIp.ip == ip)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing
        obj = ManagedIp(pool_id=pool_id, ip=ip)
        self._s.add(obj)
        await self._s.flush()
        return obj

    async def update_ip_score(
        self,
        managed_ip: ManagedIp,
        score: float,
        is_approved: bool,
        ping_rtt_ms: float | None,
        ping_loss_pct: float | None,
        tls_ok: bool | None,
        tls_handshake_ms: float | None,
        vless_ok: bool | None,
        vless_speed_mbps: float | None,
    ) -> None:
        managed_ip.score = score
        managed_ip.is_approved = is_approved
        managed_ip.ping_rtt_ms = ping_rtt_ms
        managed_ip.ping_loss_pct = ping_loss_pct
        managed_ip.tls_ok = tls_ok
        managed_ip.tls_handshake_ms = tls_handshake_ms
        managed_ip.vless_ok = vless_ok
        managed_ip.vless_speed_mbps = vless_speed_mbps
        managed_ip.last_checked_at = datetime.now(timezone.utc)
        await self._s.commit()

    async def remove_stale_ips(self, pool_id: int, current_ips: set[str]) -> int:
        """Delete IPs no longer present in any source set. Returns deleted count."""
        result = await self._s.execute(
            select(ManagedIp.ip).where(ManagedIp.pool_id == pool_id)
        )
        existing = {row[0] for row in result.fetchall()}
        to_delete = existing - current_ips
        if to_delete:
            await self._s.execute(
                delete(ManagedIp).where(
                    ManagedIp.pool_id == pool_id,
                    ManagedIp.ip.in_(to_delete),
                )
            )
            await self._s.commit()
        return len(to_delete)
```

- [ ] **Step 2: Syntax check and commit**

```bash
python -c "import ast, pathlib; ast.parse(pathlib.Path('services/managed_pool_service.py').read_text(encoding='utf-8')); print('OK')"
git add services/managed_pool_service.py
git commit -m "feat: ManagedPoolService — CRUD for managed_pools and managed_ips"
```

---

### Task 5: Remnawave API — get_vless_config_for_tag

**Files:**
- Modify: `services/remnawave_api_service.py`

**Note:** This function must be tested against the live Remnawave panel. The implementation below assumes standard Remnawave API endpoints. Adjust field names if needed after testing.

- [ ] **Step 1: Add get_users and get_vless_config_for_tag to remnawave_api_service.py**

Read the current file first, then add at the end:

```python
async def get_users(panel_url: str, api_token: str) -> list[dict]:
    """Fetch all users from Remnawave panel."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{panel_url}/api/users",
            headers={"Authorization": f"Bearer {api_token}"},
            params={"limit": 1000},
            ssl=False,
        ) as resp:
            if resp.status != 200:
                raise RemnaWaveAPIError(f"get_users failed: HTTP {resp.status}")
            data = await resp.json()
            # Remnawave returns {"users": [...]} or just [...]
            if isinstance(data, list):
                return data
            return data.get("users", [])


async def get_vless_config_for_tag(
    panel_url: str,
    api_token: str,
    service_tg_id: int,
    host_tag: str,
) -> dict | None:
    """Get VLESS outbound config JSON for a service user filtered to host_tag.

    Returns an Xray-compatible outbound dict ready to be sent to Pingachock
    with the `address` field as a placeholder (replace with test IP before sending).

    Returns None if user not found or no host with the tag.
    """
    # Step 1: find service user by Telegram ID
    users = await get_users(panel_url, api_token)
    service_user = next(
        (u for u in users if u.get("telegramId") == service_tg_id),
        None,
    )
    if not service_user:
        return None

    # Step 2: fetch hosts and find one matching the tag
    hosts = await get_hosts(panel_url, api_token)
    tagged = [h for h in hosts if host_tag in (h.get("tags") or [])]
    if not tagged:
        return None
    host = tagged[0]  # use first matching host for config template

    # Step 3: build Xray VLESS outbound JSON
    # The UUID comes from the service user's credentials.
    # Remnawave stores it as user["uuid"] or user["subCredentials"][0]["uuid"]
    user_uuid = service_user.get("uuid") or ""
    if not user_uuid and service_user.get("subCredentials"):
        user_uuid = service_user["subCredentials"][0].get("uuid", "")

    address = host.get("address", "").split(":")[0]  # placeholder, caller replaces
    port = int(host.get("port") or 443)
    network = host.get("network") or "tcp"
    security = host.get("security") or "none"

    outbound: dict = {
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": address,  # REPLACE WITH TEST IP
                "port": port,
                "users": [{"id": user_uuid, "encryption": "none"}],
            }]
        },
        "streamSettings": {
            "network": network,
            "security": security,
        },
    }

    # Carry through host-specific stream settings if present
    if host.get("tls_settings"):
        outbound["streamSettings"]["tlsSettings"] = host["tls_settings"]
    if host.get("reality_settings"):
        outbound["streamSettings"]["realitySettings"] = host["reality_settings"]
    if host.get("ws_settings") or network == "ws":
        ws_path = host.get("path") or "/"
        ws_host = host.get("host") or ""
        outbound["streamSettings"]["wsSettings"] = {
            "path": ws_path,
            "headers": {"Host": ws_host} if ws_host else {},
        }

    return outbound
```

- [ ] **Step 2: Syntax check and commit**

```bash
python -c "import ast, pathlib; ast.parse(pathlib.Path('services/remnawave_api_service.py').read_text(encoding='utf-8')); print('OK')"
git add services/remnawave_api_service.py
git commit -m "feat: remnawave_api — get_users, get_vless_config_for_tag"
```

---

### Task 6: ip_pool_scorer.py — background scoring engine

**Files:**
- Create: `services/ip_pool_scorer.py`

- [ ] **Step 1: Create services/ip_pool_scorer.py**

```python
"""Background scorer: Управляемый пул IP-адресов.

Периодически перепроверяет IP из пользовательских наборов через Pingachock:
  ping  — задержка и потери
  tls   — время хендшейка на kremnezar.online:443
  vless — работоспособность и скорость VPN через конфиг тега хостов

Итоговый score (0–100) определяет is_approved для каждого IP.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models.managed_pool import ManagedPool
from services.ip_check_service import CHECK_BATCH, distributed_ping_check, normalize_addresses
from services.ip_set_service import IpSetService
from services.managed_pool_service import ManagedPoolService
from services.pingachock_api_service import PingachockAPIError, create_check, get_check, list_checks
from services.pingachock_service import PingachockService
from services.remnawave_api_service import RemnaWaveAPIError, get_vless_config_for_tag
from services.remnawave_service import RemnaWaveService

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60          # seconds between "which pools are due?" wakeups
TLS_TARGET = "kremnezar.online"
SERVICE_TG_ID = 9636        # service user for VLESS config

_scoring_pools: set[int] = set()  # pool IDs currently being scored


# ── scoring formula ───────────────────────────────────────────────────────────

def _ping_score(rtt_ms: float | None, loss_pct: float | None) -> float:
    """0–30 points for ping quality."""
    if rtt_ms is None:
        return 10.0  # unknown but reachable — neutral
    loss = loss_pct or 0.0
    if loss > 0.5:
        return 0.0
    if rtt_ms > 300:
        base = 5.0
    elif rtt_ms > 150:
        base = 15.0
    elif rtt_ms > 80:
        base = 22.0
    else:
        base = 30.0
    # deduct 1pt per 5% loss
    loss_penalty = (loss * 100) / 5
    return max(0.0, base - loss_penalty)


def _tls_score(tls_ok: bool | None, tls_ms: float | None) -> float:
    """0–20 points for TLS handshake."""
    if not tls_ok:
        return 0.0
    if tls_ms is None:
        return 10.0
    if tls_ms > 1000:
        return 5.0
    if tls_ms > 500:
        return 10.0
    if tls_ms > 200:
        return 15.0
    return 20.0


def _vless_score(speed_mbps: float | None) -> float:
    """0–50 points for VLESS speed."""
    if speed_mbps is None:
        return 0.0
    if speed_mbps < 1:
        return 5.0
    if speed_mbps < 5:
        return 15.0
    if speed_mbps < 15:
        return 30.0
    if speed_mbps < 30:
        return 40.0
    return 50.0


def compute_score(
    ping_reachable: bool,
    ping_rtt_ms: float | None,
    ping_loss_pct: float | None,
    tls_ok: bool | None,
    tls_handshake_ms: float | None,
    vless_ok: bool | None,
    vless_speed_mbps: float | None,
) -> float:
    if not ping_reachable:
        return 0.0
    if not vless_ok:
        # VPN не работает — частичный балл
        return _ping_score(ping_rtt_ms, ping_loss_pct) * 0.5 + _tls_score(tls_ok, tls_handshake_ms) * 0.25
    return (
        _ping_score(ping_rtt_ms, ping_loss_pct)
        + _tls_score(tls_ok, tls_handshake_ms)
        + _vless_score(vless_speed_mbps)
    )


# ── TLS check helper ──────────────────────────────────────────────────────────

async def _tls_check_batch(
    api_url: str,
    api_key: str,
    ips: list[str],
    poll_timeout: float = 90.0,
    poll_interval: float = 3.0,
) -> dict[str, tuple[bool, float | None]]:
    """Check TLS handshake to TLS_TARGET:443 for each IP.

    Returns {ip: (ok, handshake_ms)}.
    Note: Pingachock tls check uses `target` as the IP and `sni` for the domain.
    """
    if not ips:
        return {}
    try:
        resp = await create_check(
            api_url, api_key, "tls", {"all": True},
            targets=ips,
            params={"port": 443, "sni": TLS_TARGET, "count": 2, "allow_insecure": True},
        )
    except PingachockAPIError as e:
        logger.warning("TLS batch check failed: %s", e)
        return {ip: (False, None) for ip in ips}

    id_to_ip: dict[str, str] = {c["id"]: c["target"] for c in resp.get("checks", [])}
    batch_id = resp.get("batch_id")
    pending = set(id_to_ip.keys())

    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_timeout
    while pending and loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            checks = await list_checks(api_url, api_key, batch_id=batch_id, limit=200)
        except PingachockAPIError:
            continue
        for c in checks:
            if c["id"] in pending and c.get("status") in ("completed", "partial", "failed", "cancelled"):
                pending.discard(c["id"])

    results: dict[str, tuple[bool, float | None]] = {}
    fetches = await asyncio.gather(
        *[get_check(api_url, api_key, cid, expand="runs") for cid in id_to_ip],
        return_exceptions=True,
    )
    for cid, fetch in zip(id_to_ip.keys(), fetches):
        ip = id_to_ip[cid]
        if isinstance(fetch, Exception):
            results[ip] = (False, None)
            continue
        ok = fetch.get("status") in ("completed", "partial")
        # Extract latency_ms from runs
        rtt: float | None = None
        for run in fetch.get("runs", []):
            r = run.get("result") or {}
            if r.get("latency_ms") is not None:
                rtt = float(r["latency_ms"])
                break
        results[ip] = (ok, rtt)
    return results


# ── VLESS check helper ────────────────────────────────────────────────────────

async def _vless_check_ip(
    api_url: str,
    api_key: str,
    ip: str,
    vless_template: dict,
    poll_timeout: float = 90.0,
    poll_interval: float = 3.0,
) -> tuple[bool, float | None]:
    """Check VLESS connectivity + speedtest for a single IP.

    Returns (ok, speed_mbps).
    """
    config = copy.deepcopy(vless_template)
    # Replace placeholder address with test IP
    try:
        config["settings"]["vnext"][0]["address"] = ip
    except (KeyError, IndexError):
        logger.warning("Invalid VLESS template structure, cannot replace IP")
        return False, None

    try:
        resp = await create_check(
            api_url, api_key, "vless", {"all": True},
            targets=[ip],
            params={"config": config},
        )
    except PingachockAPIError as e:
        logger.debug("VLESS check failed for %s: %s", ip, e)
        return False, None

    check_id = None
    for c in resp.get("checks", []):
        check_id = c["id"]
        break
    if not check_id:
        return False, None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll_timeout
    while loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            checks = await list_checks(api_url, api_key, limit=10)
        except PingachockAPIError:
            continue
        for c in checks:
            if c["id"] == check_id and c.get("status") in ("completed", "partial", "failed", "cancelled"):
                goto_fetch = True
                break
        else:
            continue
        break

    try:
        check_data = await get_check(api_url, api_key, check_id, expand="runs")
    except PingachockAPIError:
        return False, None

    ok = check_data.get("status") in ("completed", "partial")
    speed: float | None = None
    for run in check_data.get("runs", []):
        raw = (run.get("result") or {}).get("raw") or {}
        # Common field names for download speed in Pingachock vless results
        mbps = (
            raw.get("download_mbps") or raw.get("speed_mbps") or
            raw.get("download_speed") or raw.get("mbps") or raw.get("speed")
        )
        if mbps is not None:
            try:
                speed = float(mbps)
            except (ValueError, TypeError):
                pass
            break
    return ok, speed


# ── main pool scoring loop ────────────────────────────────────────────────────

async def run_ip_pool_scorer(session_factory: async_sessionmaker) -> None:
    """Entrypoint — run as asyncio.create_task in main()."""
    logger.info("IP pool scorer started")
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            due = await _get_due_pools(session_factory)
            for pool in due:
                if pool.id not in _scoring_pools:
                    asyncio.create_task(
                        _score_pool_safe(session_factory, pool.id),
                        name=f"pool-scorer-{pool.id}",
                    )
        except Exception:
            logger.exception("IP pool scorer: error in main loop")


async def _get_due_pools(session_factory: async_sessionmaker) -> list[ManagedPool]:
    async with session_factory() as session:
        svc = ManagedPoolService(session)
        pools = await svc.get_all_enabled()
    now = datetime.now(timezone.utc)
    due: list[ManagedPool] = []
    for p in pools:
        if p.last_scanned_at is None:
            due.append(p)
            continue
        last = p.last_scanned_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now >= last + timedelta(minutes=p.check_interval_minutes):
            due.append(p)
    return due


async def _score_pool_safe(session_factory: async_sessionmaker, pool_id: int) -> None:
    _scoring_pools.add(pool_id)
    try:
        await _score_pool(session_factory, pool_id)
    except Exception:
        logger.exception("IP pool scorer: error scoring pool %d", pool_id)
    finally:
        _scoring_pools.discard(pool_id)


async def _score_pool(session_factory: async_sessionmaker, pool_id: int) -> None:
    # ── load pool config ──────────────────────────────────────────────────────
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        pool = await pool_svc.get_pool_any(pool_id)
        if not pool or not pool.enabled:
            return

        ip_svc = IpSetService(session)
        sets = await ip_svc.get_sets_by_ids(list(pool.ip_set_ids))

        rw_svc = RemnaWaveService(session)
        # Find any panel for this org to use for VLESS config
        panels = await rw_svc.get_org_panels(pool.org_id)

        pc_svc = PingachockService(session)
        pc = await pc_svc.get_settings(pool.org_id)

    if not pc:
        logger.warning("Pool %d: Pingachock not configured for org %d", pool_id, pool.org_id)
        return
    if not panels:
        logger.warning("Pool %d: no panels found for org %d", pool_id, pool.org_id)
        return

    panel = panels[0]  # use first panel for VLESS config

    # ── build current IP set from source sets ─────────────────────────────────
    all_ips: list[str] = []
    for s in sets:
        extracted, _ = normalize_addresses(s.addresses)
        all_ips.extend(extracted)
    seen: set[str] = set()
    unique_ips: list[str] = []
    for ip in all_ips:
        if ip not in seen:
            seen.add(ip)
            unique_ips.append(ip)

    if not unique_ips:
        logger.info("Pool %d: no IPs in source sets", pool_id)
        return

    logger.info("Pool %d: %d unique IPs from source sets", pool_id, len(unique_ips))

    # ── sync managed_ips table ────────────────────────────────────────────────
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        removed = await pool_svc.remove_stale_ips(pool_id, set(unique_ips))
        if removed:
            logger.info("Pool %d: removed %d stale IPs", pool_id, removed)
        for ip in unique_ips:
            await pool_svc.upsert_ip(pool_id, ip)

    # ── get VLESS config template ─────────────────────────────────────────────
    vless_template: dict | None = None
    try:
        vless_template = await get_vless_config_for_tag(
            panel.url, panel.api_token, SERVICE_TG_ID, pool.host_tag
        )
    except RemnaWaveAPIError as e:
        logger.warning("Pool %d: could not get VLESS config: %s", pool_id, e)

    # ── fetch IPs that need checking ──────────────────────────────────────────
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        to_check = await pool_svc.get_ips_to_check(pool_id, pool.check_interval_minutes)

    if not to_check:
        logger.info("Pool %d: all IPs are fresh", pool_id)
        async with session_factory() as session:
            await ManagedPoolService(session).mark_scanned(pool_id)
        return

    logger.info("Pool %d: checking %d IPs", pool_id, len(to_check))
    check_ips = [m.ip for m in to_check]

    # ── ping check (batched) ──────────────────────────────────────────────────
    ping_results: dict[str, tuple[bool, float | None, float | None]] = {}
    for i in range(0, len(check_ips), CHECK_BATCH):
        batch = check_ips[i: i + CHECK_BATCH]
        try:
            batch_results = await distributed_ping_check(pc.api_url, pc.api_key, batch)
            ping_results.update(batch_results)
        except PingachockAPIError as e:
            logger.warning("Pool %d: ping batch failed: %s", pool_id, e)
            for ip in batch:
                ping_results[ip] = (False, None, None)

    # ── TLS check (batched) ───────────────────────────────────────────────────
    tls_results: dict[str, tuple[bool, float | None]] = {}
    for i in range(0, len(check_ips), CHECK_BATCH):
        batch = check_ips[i: i + CHECK_BATCH]
        batch_tls = await _tls_check_batch(pc.api_url, pc.api_key, batch)
        tls_results.update(batch_tls)

    # ── VLESS check (one by one, only reachable IPs) ──────────────────────────
    vless_results: dict[str, tuple[bool, float | None]] = {}
    if vless_template:
        reachable_ips = [ip for ip in check_ips if ping_results.get(ip, (False,))[0]]
        for ip in reachable_ips:
            vless_ok, speed = await _vless_check_ip(pc.api_url, pc.api_key, ip, vless_template)
            vless_results[ip] = (vless_ok, speed)
            logger.debug("Pool %d: %s vless=%s speed=%s", pool_id, ip, vless_ok, speed)
    else:
        logger.warning("Pool %d: skipping VLESS checks (no config)", pool_id)

    # ── compute scores and save ───────────────────────────────────────────────
    managed_by_ip = {m.ip: m for m in to_check}
    approved_count = 0
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        pool_obj = await pool_svc.get_pool_any(pool_id)
        threshold = pool_obj.score_threshold if pool_obj else 60.0

        for ip in check_ips:
            ping_ok, ping_rtt, ping_loss = ping_results.get(ip, (False, None, None))
            tls_ok, tls_ms = tls_results.get(ip, (None, None))
            vless_ok, vless_speed = vless_results.get(ip, (None, None))

            score = compute_score(ping_ok, ping_rtt, ping_loss, tls_ok, tls_ms, vless_ok, vless_speed)
            approved = score >= threshold

            managed_ip = managed_by_ip.get(ip)
            if managed_ip:
                # Re-fetch within this session
                from sqlalchemy import select
                from db.models.managed_pool import ManagedIp
                result = await session.execute(
                    select(ManagedIp).where(ManagedIp.id == managed_ip.id)
                )
                fresh = result.scalar_one_or_none()
                if fresh:
                    await pool_svc.update_ip_score(
                        fresh, score, approved,
                        ping_rtt, ping_loss, tls_ok, tls_ms, vless_ok, vless_speed,
                    )
                    if approved:
                        approved_count += 1

        await pool_svc.mark_scanned(pool_id)

    logger.info(
        "Pool %d: scoring done — %d/%d approved (threshold %.0f)",
        pool_id, approved_count, len(check_ips), threshold,
    )
```

- [ ] **Step 2: Syntax check and commit**

```bash
python -c "import ast, pathlib; ast.parse(pathlib.Path('services/ip_pool_scorer.py').read_text(encoding='utf-8')); print('OK')"
git add services/ip_pool_scorer.py
git commit -m "feat: ip_pool_scorer — background ping+TLS+VLESS scoring engine"
```

---

### Task 7: availability_monitor.py — managed pool integration

**Files:**
- Modify: `services/availability_monitor.py`

- [ ] **Step 1: Add _find_replacement_from_pool function**

Add imports at top of `availability_monitor.py`:
```python
from db.models.managed_pool import ManagedIp
from services.managed_pool_service import ManagedPoolService
```

Add the new function after `_find_replacement`:

```python
async def _find_replacement_from_pool(
    api_url: str,
    api_key: str,
    session_factory: async_sessionmaker,
    pool_id: int,
    group_id: int,
    exclude_ips: frozenset[str],
) -> tuple[str, float | None, float | None, float | None] | tuple[None, None, None, None]:
    """Get best replacement from managed pool.

    Returns (ip, loss_pct, rtt_ms, speed_mbps) or (None, None, None, None).
    Final ping check verifies IP is still alive before returning.
    """
    async with session_factory() as session:
        pool_svc = ManagedPoolService(session)
        candidates: list[ManagedIp] = await pool_svc.get_approved_ips(pool_id)

    # Filter out excluded IPs
    filtered = [c for c in candidates if c.ip not in exclude_ips]
    if not filtered:
        logger.warning("Pool %d: no approved IPs available (exclude=%d)", pool_id, len(exclude_ips))
        return None, None, None, None

    # Final liveness check in batches of CHECK_BATCH
    for i in range(0, len(filtered), CHECK_BATCH):
        if group_id in _cancel_flags:
            return None, None, None, None
        batch = filtered[i: i + CHECK_BATCH]
        batch_ips = [c.ip for c in batch]
        try:
            ping_results = await distributed_ping_check(api_url, api_key, batch_ips)
        except PingachockAPIError as e:
            logger.warning("Pool %d: final ping batch failed: %s", pool_id, e)
            continue
        for candidate in batch:
            reachable, _, _ = ping_results.get(candidate.ip, (False, None, None))
            if reachable:
                logger.info(
                    "Pool %d: replacement %s found (score=%.0f, speed=%s Mbps)",
                    pool_id, candidate.ip, candidate.score,
                    f"{candidate.vless_speed_mbps:.0f}" if candidate.vless_speed_mbps else "?",
                )
                return candidate.ip, candidate.ping_loss_pct, candidate.ping_rtt_ms, candidate.vless_speed_mbps

    logger.warning("Pool %d: all approved candidates failed final ping check", pool_id)
    return None, None, None, None
```

- [ ] **Step 2: Update _do_process_group to branch on managed_pool_id**

In `_do_process_group`, after building `pool` and `set_names`, replace the distribution section:

```python
    # ── fix bad IPs according to source mode and distribution ────────────────
    if group.distribution == "same":
        bad_ip = next(iter(all_bad_ips))
        is_dead = bad_ip in dead_ips
        current_loss_pct = all_bad_ips[bad_ip][0]

        if group.managed_pool_id:
            # Managed pool mode: get replacement from scored pool
            bad_ips_to_skip = frozenset(
                h.get("address", "").split(":")[0]
                for h in tagged_hosts if h.get("address")
            )
            cooldown_ips = _get_cooldown_ips(group.id)
            exclude = bad_ips_to_skip | cooldown_ips

            await _replace_ip_for_hosts_from_pool(
                bot=bot,
                group=group,
                panel=panel,
                pc=pc,
                session_factory=session_factory,
                hosts_to_fix=tagged_hosts,
                display_bad_ip=bad_ip,
                host_label=group.host_tag,
                member_ids=member_ids,
                reason="dead" if is_dead else "lossy",
                current_loss_pct=current_loss_pct,
                exclude_ips=exclude,
            )
        else:
            await _replace_ip_for_hosts(
                bot=bot, group=group, panel=panel, pc=pc,
                hosts_to_fix=tagged_hosts,
                display_bad_ip=bad_ip, host_label=group.host_tag,
                pool=pool, set_names=set_names, member_ids=member_ids,
                reason="dead" if is_dead else "lossy",
                current_loss_pct=current_loss_pct,
            )
    else:
        # "each" mode — silent, no Telegram notifications (unchanged)
        # ... existing each-mode code ...
```

- [ ] **Step 3: Add _replace_ip_for_hosts_from_pool function**

This is a variant of `_replace_ip_for_hosts` that uses the pool instead of live Pingachock scanning. Add after `_replace_ip_for_hosts`:

```python
async def _replace_ip_for_hosts_from_pool(
    bot: Bot,
    group,
    panel,
    pc,
    session_factory: async_sessionmaker,
    hosts_to_fix: list[dict],
    display_bad_ip: str,
    host_label: str,
    member_ids: list[int],
    reason: str,
    current_loss_pct: float | None,
    exclude_ips: frozenset[str],
) -> None:
    """Managed-pool variant: send alert, get replacement from pool, apply, edit alert."""
    import time as _time
    start_t = _time.monotonic()

    def _elapsed_str() -> str:
        secs = int(_time.monotonic() - start_t)
        m, s = divmod(secs, 60)
        return f"{m}м {s}с" if m else f"{s}с"

    if reason == "lossy":
        ip_status = (
            f"потери {current_loss_pct*100:.0f}%"
            if current_loss_pct is not None else "высокие потери"
        )
        alert_base = (
            f"📉 <b>Высокие потери пакетов</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — {ip_status}\n"
            f"Ищем замену в управляемом пуле..."
        )
    else:
        alert_base = (
            f"⚠️ <b>Недоступный IP</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — недоступен\n"
            f"Ищем замену в управляемом пуле..."
        )

    skip_kb = _skip_kb(group.id)

    async def _send_initial(chat_id: int):
        try:
            msg = await bot.send_message(
                chat_id, alert_base + "\n\nПрошло: 0с",
                parse_mode="HTML", reply_markup=skip_kb,
            )
            return (chat_id, msg.message_id)
        except Exception:
            return None

    send_results = await asyncio.gather(*[_send_initial(cid) for cid in member_ids])
    alert_msgs = [r for r in send_results if r is not None]

    # Ticker
    ticker_done: list[bool] = [False]

    async def _tick() -> None:
        while not ticker_done[0]:
            await asyncio.sleep(15)
            if ticker_done[0]:
                break
            new_text = alert_base + f"\n\nПрошло: {_elapsed_str()}"
            for chat_id, msg_id in alert_msgs:
                try:
                    await asyncio.wait_for(
                        bot.edit_message_text(
                            new_text, chat_id=chat_id, message_id=msg_id,
                            parse_mode="HTML", reply_markup=skip_kb,
                        ), timeout=6,
                    )
                except Exception:
                    pass

    ticker = asyncio.create_task(_tick())

    new_ip = new_loss_pct = new_rtt_ms = new_speed_mbps = None
    cancelled = False
    try:
        new_ip, new_loss_pct, new_rtt_ms, new_speed_mbps = await _find_replacement_from_pool(
            pc.api_url, pc.api_key, session_factory,
            group.managed_pool_id, group.id, exclude_ips,
        )
        cancelled = group.id in _cancel_flags
        if cancelled:
            _cancel_flags.discard(group.id)
    finally:
        ticker_done[0] = True
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass

    elapsed = _elapsed_str()

    if new_ip and not cancelled:
        for h in hosts_to_fix:
            try:
                await update_host_address(panel.url, panel.api_token, h["uuid"], new_ip)
            except RemnaWaveAPIError as e:
                logger.error("Group %d: failed to update host %s: %s", group.id, h["uuid"], e)

        quality_parts: list[str] = []
        if new_rtt_ms is not None:
            quality_parts.append(f"задержка {new_rtt_ms:.0f}мс")
        if new_loss_pct is not None:
            quality_parts.append(f"потери {new_loss_pct*100:.0f}%")
        if new_speed_mbps is not None:
            quality_parts.append(f"скорость {new_speed_mbps:.0f}Мбит/с")

        verb = "улучшен" if reason == "lossy" else "заменён"
        result_text = (
            f"✅ <b>Адрес {verb}</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"<code>{display_bad_ip}</code> → <code>{new_ip}</code>"
        )
        if quality_parts:
            result_text += "\n" + ", ".join(quality_parts)
        result_text += f"\n\nВремя поиска: {elapsed}"

    elif cancelled:
        result_text = (
            f"⏭ <b>Поиск отменён</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — замена не выполнена\n\n"
            f"Время поиска: {elapsed}"
        )
    else:
        result_text = (
            f"❌ <b>Замена не найдена</b>\n\n"
            f"Хост/группа: <b>{host_label}</b>\n"
            f"IP: <code>{display_bad_ip}</code> — в управляемом пуле нет подходящих адресов\n\n"
            f"Время поиска: {elapsed}"
        )

    async def _edit_final(chat_id: int, msg_id: int) -> None:
        try:
            await bot.edit_message_text(
                result_text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML",
            )
        except Exception:
            pass

    await asyncio.gather(*[_edit_final(cid, mid) for cid, mid in alert_msgs])
```

- [ ] **Step 4: Syntax check and commit**

```bash
python -c "import ast, pathlib; ast.parse(pathlib.Path('services/availability_monitor.py').read_text(encoding='utf-8')); print('OK')"
git add services/availability_monitor.py
git commit -m "feat: availability_monitor — managed pool integration, _find_replacement_from_pool"
```

---

### Task 8: Bot keyboards — new UI elements

**Files:**
- Modify: `bot/keyboards/inline.py`

- [ ] **Step 1: Add keyboards for ip_sets split and managed pool UI**

Add these functions to `bot/keyboards/inline.py`:

```python
# ── IP Sets section split ─────────────────────────────────────────────────────

def ip_sets_section_kb() -> InlineKeyboardMarkup:
    """Entry point: choose between user lists and managed pools."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Пользовательские списки", callback_data="ipset:user_lists")],
        [InlineKeyboardButton(text="⚙️ Модерируемые пулы", callback_data="mpool:list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
    ])


# ── Managed Pool keyboards ────────────────────────────────────────────────────

def mpool_list_kb(pools: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⚙️ {p.name}", callback_data=f"mpool:detail:{p.id}")]
        for p in pools
    ]
    rows.append([InlineKeyboardButton(text="➕ Создать пул", callback_data="mpool:add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:ip_sets")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mpool_detail_kb(pool_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Запустить проверку", callback_data=f"mpool:scan:{pool_id}")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"mpool:settings:{pool_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"mpool:delete_confirm:{pool_id}")],
        [InlineKeyboardButton(text="◀️ К списку пулов", callback_data="mpool:list")],
    ])


def mpool_delete_confirm_kb(pool_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"mpool:delete:{pool_id}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"mpool:detail:{pool_id}")],
    ])


def mpool_ip_sets_kb(sets: list, selected: list[int]) -> InlineKeyboardMarkup:
    """Multi-select keyboard for choosing source IP sets."""
    rows = []
    for s in sets:
        mark = "✅ " if s.id in selected else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{s.tag}",
            callback_data=f"mpool:toggle_set:{s.id}",
        )])
    rows.append([InlineKeyboardButton(text="✔️ Подтвердить выбор", callback_data="mpool:confirm_sets")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="mpool:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mpool_tags_kb(tags: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=tag, callback_data=f"mpool:tag:{i}")]
        for i, tag in enumerate(tags)
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="mpool:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mpool_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="mpool:back")],
    ])


def mpool_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать пул", callback_data="mpool:confirm_create")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="mpool:back")],
    ])


# ── Automation group source-type keyboards ────────────────────────────────────

def avail_source_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Пользовательские наборы", callback_data="avail:source:sets")],
        [InlineKeyboardButton(text="⚙️ Модерируемый пул", callback_data="avail:source:pool")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")],
    ])


def avail_pools_kb(pools: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⚙️ {p.name}", callback_data=f"avail:pool:{p.id}")]
        for p in pools
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

- [ ] **Step 2: Syntax check and commit**

```bash
python -c "import ast, pathlib; ast.parse(pathlib.Path('bot/keyboards/inline.py').read_text(encoding='utf-8')); print('OK')"
git add bot/keyboards/inline.py
git commit -m "feat: keyboards — ip_sets_section_kb, managed pool keyboards, avail source-type keyboards"
```

---

### Task 9: IP Sets handler — split menu

**Files:**
- Modify: `bot/handlers/ip_sets.py`

- [ ] **Step 1: Update menu:ip_sets handler to show split screen**

Replace the `ip_sets_menu` handler:

```python
from bot.keyboards.inline import ip_set_cancel_kb, ip_set_detail_kb, ip_sets_menu_kb, ip_sets_section_kb

@router.callback_query(F.data == "menu:ip_sets")
async def ip_sets_section(
    call: CallbackQuery,
) -> None:
    await call.answer()
    await call.message.edit_text(
        "📋 <b>Наборы IP</b>\n\n"
        "Пользовательские списки — сырые наборы адресов, загружаемые вручную.\n"
        "Модерируемые пулы — автоматически проверяемые и оцениваемые базы адресов.",
        reply_markup=ip_sets_section_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "ipset:user_lists")
async def ip_sets_menu(
    call: CallbackQuery, active_org: Organization, session: AsyncSession,
) -> None:
    await call.answer()
    await _show_menu(call, session, active_org)
```

Also update `ipset_back` to go back to the section screen:
```python
@router.callback_query(F.data == "ipset:back")
async def ipset_back(
    call: CallbackQuery, active_org: Organization, session: AsyncSession,
) -> None:
    await call.answer()
    await _show_menu(call, session, active_org)
```

- [ ] **Step 2: Syntax check and commit**

```bash
python -c "import ast, pathlib; ast.parse(pathlib.Path('bot/handlers/ip_sets.py').read_text(encoding='utf-8')); print('OK')"
git add bot/handlers/ip_sets.py
git commit -m "feat: ip_sets — split menu into section screen (user lists / managed pools)"
```

---

### Task 10: Managed Pool FSM handler

**Files:**
- Create: `bot/states/managed_pool.py`
- Create: `bot/handlers/managed_pool_fsm.py`

- [ ] **Step 1: Create bot/states/managed_pool.py**

```python
from aiogram.fsm.state import State, StatesGroup


class ManagedPoolFSM(StatesGroup):
    """FSM for creating a new managed IP pool."""
    choosing_name = State()
    choosing_ip_sets = State()
    choosing_tag = State()
    choosing_threshold = State()
    choosing_interval = State()
    confirming = State()
```

- [ ] **Step 2: Create bot/handlers/managed_pool_fsm.py**

```python
"""Handlers for Managed IP Pool (Модерируемые пулы).

UI flow:
  menu:ip_sets → mpool:list → mpool:detail:{id}
  mpool:add → ManagedPoolFSM.choosing_name
            → ManagedPoolFSM.choosing_ip_sets (multi-select)
            → ManagedPoolFSM.choosing_tag
            → ManagedPoolFSM.choosing_threshold
            → ManagedPoolFSM.choosing_interval
            → ManagedPoolFSM.confirming
            → mpool:confirm_create → save
"""
from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    mpool_cancel_kb,
    mpool_confirm_kb,
    mpool_delete_confirm_kb,
    mpool_detail_kb,
    mpool_ip_sets_kb,
    mpool_list_kb,
    mpool_tags_kb,
)
from bot.states.managed_pool import ManagedPoolFSM
from db.models.organization import Organization
from db.models.user import User
from services.audit_service import send_audit
from services.ip_set_service import IpSetService
from services.managed_pool_service import ManagedPoolService
from services.remnawave_api_service import RemnaWaveAPIError, get_hosts
from services.remnawave_service import RemnaWaveService

router = Router()

SERVICE_TG_ID = 9636


def _extract_tags(hosts: list[dict]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for host in hosts:
        for tag in host.get("tags") or []:
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
    return sorted(tags)


async def _show_pool_list(call: CallbackQuery, session: AsyncSession, active_org: Organization) -> None:
    svc = ManagedPoolService(session)
    pools = await svc.get_org_pools(active_org.id)
    text = (
        "⚙️ <b>Модерируемые пулы</b>\n\n"
        + (
            "\n".join(f"• {p.name}" for p in pools)
            if pools else "Пулов пока нет. Создайте первый!"
        )
    )
    await call.message.edit_text(text, reply_markup=mpool_list_kb(pools), parse_mode="HTML")


# ── list ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "mpool:list")
async def mpool_list(call: CallbackQuery, session: AsyncSession, active_org: Organization) -> None:
    await call.answer()
    await _show_pool_list(call, session, active_org)


# ── detail ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^mpool:detail:\d+$"))
async def mpool_detail(call: CallbackQuery, session: AsyncSession, active_org: Organization) -> None:
    await call.answer()
    pool_id = int(call.data.split(":")[2])
    svc = ManagedPoolService(session)
    pool = await svc.get_pool(pool_id, active_org.id)
    if not pool:
        await call.answer("Пул не найден.", show_alert=True)
        return

    stats = await svc.get_pool_stats(pool_id)
    ip_svc = IpSetService(session)
    sets = await ip_svc.get_sets_by_ids(list(pool.ip_set_ids))
    sets_label = ", ".join(s.tag for s in sets) if sets else "—"

    last = "ещё не проверялся"
    if pool.last_scanned_at:
        from datetime import timezone
        ts = pool.last_scanned_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        last = ts.strftime("%Y-%m-%d %H:%M UTC")

    total = stats["total"]
    approved = stats["approved"]
    pct = f"{approved/total*100:.0f}%" if total else "—"

    await call.message.edit_text(
        f"⚙️ <b>Пул «{pool.name}»</b>\n\n"
        f"Источники: <b>{sets_label}</b>\n"
        f"Тег хостов: <b>{pool.host_tag}</b>\n"
        f"Порог: {pool.score_threshold:.0f} | Интервал: {pool.check_interval_minutes} мин\n\n"
        f"📊 Всего: {total} | Одобрено: {approved} ({pct}) | "
        f"Ожидают: {stats['pending']} | Отклонено: {stats['rejected']}\n\n"
        f"Последняя проверка: {last}",
        reply_markup=mpool_detail_kb(pool_id),
        parse_mode="HTML",
    )


# ── manual scan trigger ───────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^mpool:scan:\d+$"))
async def mpool_scan(call: CallbackQuery, session: AsyncSession, active_org: Organization) -> None:
    pool_id = int(call.data.split(":")[2])
    svc = ManagedPoolService(session)
    pool = await svc.get_pool(pool_id, active_org.id)
    if not pool:
        await call.answer("Пул не найден.", show_alert=True)
        return
    # Reset last_scanned_at to trigger scorer on next cycle
    from sqlalchemy import update
    from db.models.managed_pool import ManagedPool
    await session.execute(
        update(ManagedPool).where(ManagedPool.id == pool_id).values(last_scanned_at=None)
    )
    await session.commit()
    await call.answer("🔄 Проверка запущена. Результаты появятся через несколько минут.", show_alert=True)


# ── delete ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^mpool:delete_confirm:\d+$"))
async def mpool_delete_confirm(call: CallbackQuery, session: AsyncSession, active_org: Organization) -> None:
    await call.answer()
    pool_id = int(call.data.split(":")[2])
    svc = ManagedPoolService(session)
    pool = await svc.get_pool(pool_id, active_org.id)
    if not pool:
        await call.answer("Пул не найден.", show_alert=True)
        return
    await call.message.edit_text(
        f"🗑 Удалить пул <b>«{pool.name}»</b>?\n\nВсе оценённые IP будут удалены.",
        reply_markup=mpool_delete_confirm_kb(pool_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^mpool:delete:\d+$"))
async def mpool_delete(
    call: CallbackQuery, session: AsyncSession, active_org: Organization, db_user: User,
) -> None:
    await call.answer()
    pool_id = int(call.data.split(":")[2])
    svc = ManagedPoolService(session)
    pool = await svc.get_pool(pool_id, active_org.id)
    if not pool:
        await call.answer("Пул не найден.", show_alert=True)
        return
    name = pool.name
    await svc.delete_pool(pool_id, active_org.id)
    send_audit(call.bot, active_org.id, db_user, f"Удалил управляемый пул: {name}")
    await call.answer(f"✅ Пул «{name}» удалён.", show_alert=False)
    await _show_pool_list(call, session, active_org)


# ── FSM: create new pool ──────────────────────────────────────────────────────

@router.callback_query(F.data == "mpool:add")
async def mpool_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(ManagedPoolFSM.choosing_name)
    await call.message.edit_text(
        "➕ <b>Новый модерируемый пул</b>\n\nВведите название пула:",
        reply_markup=mpool_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(ManagedPoolFSM.choosing_name)
async def mpool_got_name(
    message: Message, state: FSMContext, session: AsyncSession, active_org: Organization,
) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 200:
        await message.answer("Название должно быть от 1 до 200 символов:", reply_markup=mpool_cancel_kb())
        return

    ip_svc = IpSetService(session)
    sets = await ip_svc.get_org_sets(active_org.id)
    if not sets:
        await message.answer(
            "❌ Нет пользовательских наборов IP.\n\nСначала добавьте набор в «Пользовательские списки».",
            reply_markup=mpool_cancel_kb(),
        )
        return

    sets_info = [{"id": s.id, "tag": s.tag} for s in sets]
    await state.update_data(name=name, sets_info=sets_info, selected_set_ids=[])
    await state.set_state(ManagedPoolFSM.choosing_ip_sets)
    await message.answer(
        f"Пул: <b>{name}</b>\n\nВыберите пользовательские наборы IP для этого пула:",
        reply_markup=mpool_ip_sets_kb(sets, []),
        parse_mode="HTML",
    )


@router.callback_query(ManagedPoolFSM.choosing_ip_sets, F.data.regexp(r"^mpool:toggle_set:\d+$"))
async def mpool_toggle_set(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    set_id = int(call.data.split(":")[2])
    data = await state.get_data()
    selected: list[int] = list(data.get("selected_set_ids", []))
    if set_id in selected:
        selected.remove(set_id)
    else:
        selected.append(set_id)
    await state.update_data(selected_set_ids=selected)
    sets_info = data["sets_info"]
    # Reconstruct IpSet-like objects for keyboard
    class _S:
        def __init__(self, d): self.id = d["id"]; self.tag = d["tag"]
    await call.message.edit_reply_markup(
        reply_markup=mpool_ip_sets_kb([_S(s) for s in sets_info], selected)
    )


@router.callback_query(ManagedPoolFSM.choosing_ip_sets, F.data == "mpool:confirm_sets")
async def mpool_confirm_sets(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, active_org: Organization,
) -> None:
    await call.answer()
    data = await state.get_data()
    selected: list[int] = data.get("selected_set_ids", [])
    if not selected:
        await call.answer("Выберите хотя бы один набор.", show_alert=True)
        return

    # Load tags from all panels
    rw_svc = RemnaWaveService(session)
    panels = await rw_svc.get_org_panels(active_org.id)
    all_tags: list[str] = []
    for panel in panels:
        try:
            hosts = await get_hosts(panel.url, panel.api_token)
            all_tags.extend(_extract_tags(hosts))
        except RemnaWaveAPIError:
            pass
    tags = sorted(set(all_tags))

    if not tags:
        await call.answer("Нет доступных тегов хостов.", show_alert=True)
        return

    await state.update_data(tags=tags)
    await state.set_state(ManagedPoolFSM.choosing_tag)
    await call.message.edit_text(
        "Выберите тег хостов для VLESS-проверки (конфиг берётся из этого тега):",
        reply_markup=mpool_tags_kb(tags),
    )


@router.callback_query(ManagedPoolFSM.choosing_tag, F.data.regexp(r"^mpool:tag:\d+$"))
async def mpool_tag_chosen(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    tags: list[str] = data["tags"]
    if idx >= len(tags):
        await call.answer("Тег не найден.", show_alert=True)
        return
    tag = tags[idx]
    await state.update_data(host_tag=tag)
    await state.set_state(ManagedPoolFSM.choosing_threshold)
    await call.message.edit_text(
        f"Тег: <b>{tag}</b>\n\n"
        "Минимальный балл для одобрения IP (0–100).\n"
        "Рекомендуется: 60 (только с рабочим VLESS и хорошим пингом).\n\n"
        "Введите число или нажмите /skip для значения по умолчанию (60):",
        reply_markup=mpool_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(ManagedPoolFSM.choosing_threshold)
async def mpool_got_threshold(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == "/skip":
        threshold = 60.0
    else:
        try:
            threshold = float(text)
            if not (0 <= threshold <= 100):
                raise ValueError
        except ValueError:
            await message.answer("Введите число от 0 до 100 (или /skip для 60):", reply_markup=mpool_cancel_kb())
            return
    await state.update_data(threshold=threshold)
    await state.set_state(ManagedPoolFSM.choosing_interval)
    await message.answer(
        f"Порог: <b>{threshold:.0f}</b>\n\n"
        "Интервал пересканирования пула (в минутах).\n"
        "Введите число (минимум 30) или /skip для 120 мин:",
        reply_markup=mpool_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(ManagedPoolFSM.choosing_interval)
async def mpool_got_interval(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == "/skip":
        interval = 120
    else:
        try:
            interval = int(text)
            if interval < 30:
                raise ValueError
        except ValueError:
            await message.answer("Введите целое число ≥ 30 (или /skip для 120):", reply_markup=mpool_cancel_kb())
            return
    await state.update_data(interval=interval)
    await state.set_state(ManagedPoolFSM.confirming)
    await _show_confirm(message, state)


async def _show_confirm(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    name = data.get("name", "?")
    host_tag = data.get("host_tag", "?")
    sets_info: list[dict] = data.get("sets_info", [])
    selected_ids: list[int] = data.get("selected_set_ids", [])
    threshold: float = data.get("threshold", 60.0)
    interval: int = data.get("interval", 120)

    selected_sets = [s for s in sets_info if s["id"] in selected_ids]
    sets_label = ", ".join(s["tag"] for s in selected_sets) or "—"

    text = (
        f"✅ <b>Готово к созданию</b>\n\n"
        f"Название: <b>{name}</b>\n"
        f"Наборы IP: <b>{sets_label}</b>\n"
        f"Тег хостов: <b>{host_tag}</b>\n"
        f"Порог одобрения: <b>{threshold:.0f}</b>\n"
        f"Интервал проверки: <b>{interval} мин</b>"
    )
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer(text, reply_markup=mpool_confirm_kb(), parse_mode="HTML")


@router.callback_query(ManagedPoolFSM.confirming, F.data == "mpool:confirm_create")
async def mpool_confirm_create(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    active_org: Organization, db_user: User,
) -> None:
    await call.answer()
    data = await state.get_data()
    svc = ManagedPoolService(session)
    pool = await svc.create_pool(
        org_id=active_org.id,
        name=data["name"],
        host_tag=data["host_tag"],
        ip_set_ids=data.get("selected_set_ids", []),
        score_threshold=data.get("threshold", 60.0),
        check_interval_minutes=data.get("interval", 120),
    )
    await state.clear()
    send_audit(call.bot, active_org.id, db_user, f"Создал управляемый пул: {pool.name}")
    await call.answer("✅ Пул создан!", show_alert=False)
    await _show_pool_list(call, session, active_org)


# ── FSM back navigation ───────────────────────────────────────────────────────

@router.callback_query(F.data == "mpool:back")
async def mpool_back(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, active_org: Organization,
) -> None:
    await call.answer()
    current = await state.get_state()
    data = await state.get_data()

    if current is None or current == ManagedPoolFSM.choosing_name:
        await state.clear()
        await _show_pool_list(call, session, active_org)

    elif current == ManagedPoolFSM.choosing_ip_sets:
        await state.set_state(ManagedPoolFSM.choosing_name)
        await call.message.edit_text(
            "➕ <b>Новый модерируемый пул</b>\n\nВведите название пула:",
            reply_markup=mpool_cancel_kb(),
            parse_mode="HTML",
        )

    elif current == ManagedPoolFSM.choosing_tag:
        sets_info = data.get("sets_info", [])
        selected = data.get("selected_set_ids", [])
        class _S:
            def __init__(self, d): self.id = d["id"]; self.tag = d["tag"]
        await state.set_state(ManagedPoolFSM.choosing_ip_sets)
        await call.message.edit_text(
            "Выберите пользовательские наборы IP:",
            reply_markup=mpool_ip_sets_kb([_S(s) for s in sets_info], selected),
        )

    elif current == ManagedPoolFSM.choosing_threshold:
        tags = data.get("tags", [])
        await state.set_state(ManagedPoolFSM.choosing_tag)
        await call.message.edit_text(
            "Выберите тег хостов для VLESS-проверки:",
            reply_markup=mpool_tags_kb(tags),
        )

    elif current == ManagedPoolFSM.choosing_interval:
        threshold = data.get("threshold", 60.0)
        await state.set_state(ManagedPoolFSM.choosing_threshold)
        await call.message.edit_text(
            f"Текущий порог: {threshold:.0f}\n\nВведите минимальный балл (0–100) или /skip для 60:",
            reply_markup=mpool_cancel_kb(),
        )

    elif current == ManagedPoolFSM.confirming:
        await state.set_state(ManagedPoolFSM.choosing_interval)
        await call.message.edit_text(
            "Интервал пересканирования (мин, минимум 30) или /skip для 120:",
            reply_markup=mpool_cancel_kb(),
        )

    else:
        await state.clear()
        await _show_pool_list(call, session, active_org)
```

- [ ] **Step 3: Syntax check and commit**

```bash
python -c "import ast, pathlib; [ast.parse(pathlib.Path(f).read_text(encoding='utf-8')) for f in ['bot/states/managed_pool.py','bot/handlers/managed_pool_fsm.py']]; print('OK')"
git add bot/states/managed_pool.py bot/handlers/managed_pool_fsm.py
git commit -m "feat: managed_pool FSM handler and states"
```

---

### Task 11: Automation FSM — add source type step

**Files:**
- Modify: `bot/states/automation.py`
- Modify: `bot/handlers/automation_fsm.py`

- [ ] **Step 1: Add choosing_source_type and choosing_pool states**

In `bot/states/automation.py`:

```python
class AvailGroupFSM(StatesGroup):
    """FSM for creating a new 'Поддержание доступности IP' automation group."""
    choosing_panel = State()
    choosing_tag = State()
    choosing_source_type = State()   # NEW: sets or pool
    choosing_ip_sets = State()       # multi-select with checkmarks (sets mode)
    choosing_pool = State()          # NEW: select managed pool (pool mode)
    choosing_distribution = State()
    choosing_interval = State()
    confirming = State()
```

- [ ] **Step 2: Update automation_fsm.py**

Add import at top:
```python
from bot.keyboards.inline import (
    ...,
    avail_pools_kb,
    avail_source_type_kb,
)
from services.managed_pool_service import ManagedPoolService
```

After `avail_tag_chosen` (step 2), replace the direct jump to `choosing_ip_sets` with `choosing_source_type`:

```python
@router.callback_query(AvailGroupFSM.choosing_tag, F.data.regexp(r"^avail:tag:\d+$"))
async def avail_tag_chosen(
    call: CallbackQuery, state: FSMContext,
    session: AsyncSession, active_org: Organization,
) -> None:
    await call.answer()
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    tags: list[str] = data["tags"]
    if idx >= len(tags):
        await call.answer("Тег не найден.", show_alert=True)
        return
    tag = tags[idx]
    await state.update_data(selected_tag=tag)
    await state.set_state(AvailGroupFSM.choosing_source_type)
    await call.message.edit_text(
        f"Панель: <b>{data['panel_tag']}</b>  |  Тег: <b>{tag}</b>\n\n"
        "Источник IP-адресов для замен:",
        reply_markup=avail_source_type_kb(),
        parse_mode="HTML",
    )
```

Add handler for source type choice:

```python
@router.callback_query(AvailGroupFSM.choosing_source_type, F.data == "avail:source:sets")
async def avail_source_sets(
    call: CallbackQuery, state: FSMContext,
    session: AsyncSession, active_org: Organization,
) -> None:
    await call.answer()
    data = await state.get_data()
    ip_svc = IpSetService(session)
    sets = await ip_svc.get_org_sets(active_org.id)
    if not sets:
        await call.message.edit_text(
            "❌ Нет сохранённых наборов IP.\n\nСначала добавьте набор в «Наборы IP».",
            reply_markup=avail_source_type_kb(),
        )
        return
    sets_info = [{"id": s.id, "tag": s.tag, "count": len(s.addresses.splitlines())} for s in sets]
    await state.update_data(
        source_type="sets", sets_info=sets_info, selected_set_ids=[],
        managed_pool_id=None,
    )
    await state.set_state(AvailGroupFSM.choosing_ip_sets)
    await call.message.edit_text(
        f"Панель: <b>{data['panel_tag']}</b>  |  Тег: <b>{data['selected_tag']}</b>\n\n"
        "Выберите один или несколько наборов IP:",
        reply_markup=avail_ip_sets_kb(sets_info, []),
        parse_mode="HTML",
    )


@router.callback_query(AvailGroupFSM.choosing_source_type, F.data == "avail:source:pool")
async def avail_source_pool(
    call: CallbackQuery, state: FSMContext,
    session: AsyncSession, active_org: Organization,
) -> None:
    await call.answer()
    svc = ManagedPoolService(session)
    pools = await svc.get_org_pools(active_org.id)
    if not pools:
        await call.message.edit_text(
            "❌ Нет модерируемых пулов.\n\nСоздайте пул в «Наборы IP → Модерируемые пулы».",
            reply_markup=avail_source_type_kb(),
        )
        return
    await state.update_data(source_type="pool", selected_set_ids=[], managed_pool_id=None)
    await state.set_state(AvailGroupFSM.choosing_pool)
    await call.message.edit_text(
        "Выберите управляемый пул:",
        reply_markup=avail_pools_kb(pools),
    )


@router.callback_query(AvailGroupFSM.choosing_pool, F.data.regexp(r"^avail:pool:\d+$"))
async def avail_pool_chosen(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    pool_id = int(call.data.split(":")[2])
    await state.update_data(managed_pool_id=pool_id)
    await state.set_state(AvailGroupFSM.choosing_distribution)
    await call.message.edit_text(
        "Как заменять IP хостам?",
        reply_markup=avail_distribution_kb(),
    )
```

Update `_show_confirm` to display pool vs sets:

```python
async def _show_confirm(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    ...
    source_type = data.get("source_type", "sets")
    managed_pool_id = data.get("managed_pool_id")

    if source_type == "pool" and managed_pool_id:
        source_label = f"  • Управляемый пул ID {managed_pool_id}"
    else:
        selected_sets = [s for s in sets_info if s["id"] in selected_ids]
        source_label = "\n".join(
            f"  • {s['tag']} ({s.get('count', 0):,} записей)" for s in selected_sets
        ) or "  —"

    await call.message.edit_text(
        f"✅ <b>Готово к сохранению</b>\n\n"
        f"Панель: <b>{panel_tag}</b>\n"
        f"Тег хостов: <b>{selected_tag}</b>\n"
        f"Источник IP:\n{source_label}\n"
        f"Режим: {dist_label}\n"
        f"Интервал: каждые <b>{interval} мин</b>",
        reply_markup=avail_confirm_kb(),
        parse_mode="HTML",
    )
```

Update `avail_confirm_create` to save `managed_pool_id`:

```python
    auto_svc = AutomationService(session)
    managed_pool_id: int | None = data.get("managed_pool_id")
    await auto_svc.add_group(
        org_id=active_org.id,
        panel_id=panel_id,
        host_tag=selected_tag,
        ip_set_ids=selected_set_ids if not managed_pool_id else [],
        distribution=distribution,
        interval_minutes=interval_minutes,
        managed_pool_id=managed_pool_id,
    )
```

Update `AutomationService.add_group` signature to accept `managed_pool_id`:
In `services/automation_service.py`, add `managed_pool_id: int | None = None` param and pass to model.

Update `avail_group_detail` to show pool info if set:
```python
    if group.managed_pool_id:
        source_label = f"Управляемый пул ID {group.managed_pool_id}"
    else:
        sets_label = ", ".join(s.tag for s in sets) if sets else "—"
        source_label = f"Наборы IP: {sets_label}"
```

Update `avail:back` handler to handle `choosing_source_type` and `choosing_pool` states.

- [ ] **Step 3: Syntax check and commit**

```bash
python -c "import ast, pathlib; [ast.parse(pathlib.Path(f).read_text(encoding='utf-8')) for f in ['bot/states/automation.py','bot/handlers/automation_fsm.py']]; print('OK')"
git add bot/states/automation.py bot/handlers/automation_fsm.py
git commit -m "feat: automation FSM — source type step (user sets / managed pool)"
```

---

### Task 12: Wire everything in main.py + AutomationService update

**Files:**
- Modify: `main.py`
- Modify: `services/automation_service.py`

- [ ] **Step 1: Update AutomationService.add_group to accept managed_pool_id**

Read `services/automation_service.py`, find `add_group`, add `managed_pool_id: int | None = None` parameter and pass it to the `AutomationGroup(...)` constructor.

- [ ] **Step 2: Update main.py**

```python
from bot.handlers import (
    ...,
    managed_pool_fsm,   # ADD
)
from services.ip_pool_scorer import run_ip_pool_scorer  # ADD

# In main():
dp.include_router(managed_pool_fsm.router)   # ADD (before common.router)

pool_scorer_task = asyncio.create_task(      # ADD
    run_ip_pool_scorer(async_session_factory)
)
# In finally block, cancel pool_scorer_task same as others
```

- [ ] **Step 3: Syntax check all modified files**

```bash
python -c "
import ast, pathlib
files = [
  'main.py', 'services/automation_service.py',
  'services/ip_pool_scorer.py', 'services/availability_monitor.py',
]
for f in files:
    ast.parse(pathlib.Path(f).read_text(encoding='utf-8'))
    print('OK', f)
"
```

- [ ] **Step 4: Commit and push**

```bash
git add main.py services/automation_service.py
git commit -m "feat: wire managed_pool_fsm router and ip_pool_scorer task in main"
git push
```

---

### Task 13: Deploy and smoke test

- [ ] **Step 1: Apply migration on server**

```bash
git pull
docker compose up -d --build bot
docker compose exec bot alembic upgrade head
```

- [ ] **Step 2: Smoke test checklist**

1. Open bot → main menu → "📋 Наборы IP" → должен появиться экран выбора с двумя кнопками
2. → "📋 Пользовательские списки" → существующие наборы отображаются
3. Создать новый набор, вставив текст с "мусором" вокруг IP (JSON, комментарии) → IP извлекаются корректно
4. → "⚙️ Модерируемые пулы" → экран создания пула
5. Создать пул: выбрать набор, тег, порог 60, интервал 30 мин
6. Подождать 30+ минут → зайти в детали пула → статистика обновилась
7. Создать автоматизацию → на шаге "Источник IP" выбрать "Управляемый пул" → выбрать созданный пул → сохранить
8. Проверить что в деталях группы отображается "Управляемый пул ID X"

- [ ] **Step 3: Test VLESS config fetch (важно!)**

После запуска, в логах бота найти строку:
```
Pool N: could not get VLESS config
```
Если она есть — `get_vless_config_for_tag` не нашла пользователя или хосты. Изучить ответ Remnawave API и скорректировать поля в `remnawave_api_service.py` (поле `telegramId`, структура ответа `/api/users`, поля хоста).

- [ ] **Step 4: Final commit**

```bash
git push
```
