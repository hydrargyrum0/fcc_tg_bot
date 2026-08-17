"""DB service for Lightsail IP search configs and static IPs."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.lightsail_search import LightsailRegionConfig, LightsailStaticIp


class LightsailSearchService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # ── configs ───────────────────────────────────────────────────────────────

    async def get_config(
        self, aws_account_id: int, region: str
    ) -> LightsailRegionConfig | None:
        res = await self._s.execute(
            select(LightsailRegionConfig).where(
                LightsailRegionConfig.aws_account_id == aws_account_id,
                LightsailRegionConfig.region == region,
            )
        )
        return res.scalar_one_or_none()

    async def get_config_by_id(self, config_id: int) -> LightsailRegionConfig | None:
        res = await self._s.execute(
            select(LightsailRegionConfig).where(LightsailRegionConfig.id == config_id)
        )
        return res.scalar_one_or_none()

    async def get_org_configs(self, org_id: int) -> list[LightsailRegionConfig]:
        res = await self._s.execute(
            select(LightsailRegionConfig)
            .where(LightsailRegionConfig.org_id == org_id)
            .order_by(LightsailRegionConfig.created_at)
        )
        return list(res.scalars().all())

    async def get_account_configs(
        self, aws_account_id: int
    ) -> list[LightsailRegionConfig]:
        res = await self._s.execute(
            select(LightsailRegionConfig)
            .where(LightsailRegionConfig.aws_account_id == aws_account_id)
            .order_by(LightsailRegionConfig.region)
        )
        return list(res.scalars().all())

    async def get_all_searching(self) -> list[LightsailRegionConfig]:
        """Return all configs currently in 'searching' status (for worker startup)."""
        res = await self._s.execute(
            select(LightsailRegionConfig).where(
                LightsailRegionConfig.status == "searching"
            )
        )
        return list(res.scalars().all())

    async def get_all_monitoring(self) -> list[LightsailRegionConfig]:
        """Return all configs in 'monitoring' status (for periodic recheck)."""
        res = await self._s.execute(
            select(LightsailRegionConfig).where(
                LightsailRegionConfig.status == "monitoring"
            )
        )
        return list(res.scalars().all())

    async def upsert_config(
        self,
        org_id: int,
        aws_account_id: int,
        region: str,
        region_display_name: str = "",
    ) -> LightsailRegionConfig:
        existing = await self.get_config(aws_account_id, region)
        if existing:
            return existing
        cfg = LightsailRegionConfig(
            org_id=org_id,
            aws_account_id=aws_account_id,
            region=region,
            region_display_name=region_display_name,
        )
        self._s.add(cfg)
        await self._s.commit()
        await self._s.refresh(cfg)
        return cfg

    async def set_status(self, config_id: int, status: str) -> None:
        cfg = await self.get_config_by_id(config_id)
        if cfg:
            cfg.status = status
            await self._s.commit()

    async def mark_search_started(self, config_id: int, instance_name: str) -> None:
        cfg = await self.get_config_by_id(config_id)
        if cfg:
            cfg.status = "searching"
            cfg.instance_name = instance_name
            cfg.search_started_at = datetime.utcnow()
            cfg.total_checked = 0
            await self._s.commit()

    async def mark_search_stopped(self, config_id: int) -> None:
        """Clear instance_name and set status to 'idle' or 'monitoring' depending on results."""
        cfg = await self.get_config_by_id(config_id)
        if not cfg:
            return
        cfg.instance_name = None
        working = await self.get_working_ips(config_id)
        cfg.status = "monitoring" if len(working) >= cfg.target_count else "idle"
        await self._s.commit()

    async def mark_paused(self, config_id: int) -> None:
        cfg = await self.get_config_by_id(config_id)
        if cfg:
            cfg.status = "paused"
            cfg.instance_name = None
            await self._s.commit()

    async def increment_checked(self, config_id: int) -> None:
        cfg = await self.get_config_by_id(config_id)
        if cfg:
            cfg.total_checked = (cfg.total_checked or 0) + 1
            await self._s.commit()

    async def update_target(self, config_id: int, target_count: int) -> None:
        cfg = await self.get_config_by_id(config_id)
        if cfg:
            cfg.target_count = max(1, min(5, target_count))
            await self._s.commit()

    async def update_recheck(self, config_id: int, recheck_minutes: int) -> None:
        cfg = await self.get_config_by_id(config_id)
        if cfg:
            cfg.recheck_minutes = max(10, recheck_minutes)
            await self._s.commit()

    async def update_node_ids(
        self, config_id: int, node_ids: list[str] | None
    ) -> None:
        cfg = await self.get_config_by_id(config_id)
        if cfg:
            cfg.node_ids = node_ids if node_ids else None
            await self._s.commit()

    async def mark_recheck(self, config_id: int) -> None:
        cfg = await self.get_config_by_id(config_id)
        if cfg:
            cfg.last_recheck_at = datetime.utcnow()
            await self._s.commit()

    # ── static IPs ────────────────────────────────────────────────────────────

    async def get_all_ips(self, config_id: int) -> list[LightsailStaticIp]:
        res = await self._s.execute(
            select(LightsailStaticIp)
            .where(LightsailStaticIp.config_id == config_id)
            .order_by(LightsailStaticIp.created_at)
        )
        return list(res.scalars().all())

    async def get_working_ips(self, config_id: int) -> list[LightsailStaticIp]:
        res = await self._s.execute(
            select(LightsailStaticIp).where(
                LightsailStaticIp.config_id == config_id,
                LightsailStaticIp.is_working == True,  # noqa: E712
            )
        )
        return list(res.scalars().all())

    async def add_static_ip(
        self,
        config_id: int,
        static_ip_name: str,
        ip_address: str,
        is_attached: bool = False,
    ) -> LightsailStaticIp:
        row = LightsailStaticIp(
            config_id=config_id,
            static_ip_name=static_ip_name,
            ip_address=ip_address,
            is_working=None,
            is_attached=is_attached,
        )
        self._s.add(row)
        await self._s.commit()
        await self._s.refresh(row)
        return row

    async def set_ip_result(
        self, static_ip_name: str, is_working: bool, is_attached: bool = False
    ) -> None:
        res = await self._s.execute(
            select(LightsailStaticIp).where(
                LightsailStaticIp.static_ip_name == static_ip_name
            )
        )
        row = res.scalar_one_or_none()
        if row:
            row.is_working = is_working
            row.is_attached = is_attached
            row.tested_at = datetime.utcnow()
            await self._s.commit()

    async def set_ip_attached(self, static_ip_name: str, is_attached: bool) -> None:
        res = await self._s.execute(
            select(LightsailStaticIp).where(
                LightsailStaticIp.static_ip_name == static_ip_name
            )
        )
        row = res.scalar_one_or_none()
        if row:
            row.is_attached = is_attached
            await self._s.commit()

    async def delete_static_ip(self, static_ip_name: str) -> None:
        res = await self._s.execute(
            select(LightsailStaticIp).where(
                LightsailStaticIp.static_ip_name == static_ip_name
            )
        )
        row = res.scalar_one_or_none()
        if row:
            await self._s.delete(row)
            await self._s.commit()

    async def delete_all_non_working(self, config_id: int) -> list[str]:
        """Delete all non-working static IP rows and return their names."""
        res = await self._s.execute(
            select(LightsailStaticIp).where(
                LightsailStaticIp.config_id == config_id,
                LightsailStaticIp.is_working == False,  # noqa: E712
            )
        )
        rows = list(res.scalars().all())
        names = [r.static_ip_name for r in rows]
        for row in rows:
            await self._s.delete(row)
        if rows:
            await self._s.commit()
        return names
