from __future__ import annotations

from datetime import datetime, timedelta

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
        vless_service_short_uuid: str | None = None,
    ) -> ManagedPool:
        pool = ManagedPool(
            org_id=org_id,
            name=name,
            host_tag=host_tag,
            ip_set_ids=ip_set_ids,
            score_threshold=score_threshold,
            check_interval_minutes=check_interval_minutes,
            vless_service_short_uuid=vless_service_short_uuid,
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
            .values(last_scanned_at=datetime.utcnow())
        )
        await self._s.commit()

    async def reset_last_scanned(self, pool_id: int) -> None:
        """Force re-scan on next scorer cycle by clearing last_scanned_at."""
        await self._s.execute(
            update(ManagedPool)
            .where(ManagedPool.id == pool_id)
            .values(last_scanned_at=None)
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

    async def get_all_ips(self, pool_id: int) -> list[ManagedIp]:
        """All IPs for the pool: approved first (score desc), then pending, then rejected."""
        result = await self._s.execute(
            select(ManagedIp)
            .where(ManagedIp.pool_id == pool_id)
            .order_by(
                ManagedIp.is_approved.desc(),
                ManagedIp.score.desc().nullslast(),
                ManagedIp.ip,
            )
        )
        return list(result.scalars().all())

    async def get_approved_ips(self, pool_id: int) -> list[ManagedIp]:
        """Approved IPs sorted by score descending — used by availability monitor."""
        result = await self._s.execute(
            select(ManagedIp)
            .where(ManagedIp.pool_id == pool_id, ManagedIp.is_approved == True)  # noqa: E712
            .order_by(ManagedIp.score.desc())
        )
        return list(result.scalars().all())

    async def get_ips_to_check(self, pool_id: int, stale_after_minutes: int) -> list[ManagedIp]:
        """IPs that have never been checked or whose last_checked_at is stale."""
        cutoff = datetime.utcnow() - timedelta(minutes=stale_after_minutes)
        result = await self._s.execute(
            select(ManagedIp).where(
                ManagedIp.pool_id == pool_id,
                (ManagedIp.last_checked_at == None)  # noqa: E711
                | (ManagedIp.last_checked_at < cutoff),
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
        managed_ip.last_checked_at = datetime.utcnow()
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
