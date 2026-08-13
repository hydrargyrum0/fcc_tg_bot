"""DB service for automation groups (one per org/tag combination)."""
from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.automation_group import AutomationGroup


class AutomationService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_org_groups(self, org_id: int) -> list[AutomationGroup]:
        result = await self._s.execute(
            select(AutomationGroup)
            .where(AutomationGroup.org_id == org_id)
            .order_by(AutomationGroup.created_at)
        )
        return list(result.scalars().all())

    async def get_group(self, group_id: int, org_id: int) -> AutomationGroup | None:
        result = await self._s.execute(
            select(AutomationGroup).where(
                AutomationGroup.id == group_id,
                AutomationGroup.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_all_enabled(self) -> list[AutomationGroup]:
        """All enabled groups across all orgs — used by background monitor."""
        result = await self._s.execute(
            select(AutomationGroup).where(AutomationGroup.enabled.is_(True))
        )
        return list(result.scalars().all())

    async def add_group(
        self,
        org_id: int,
        panel_id: int,
        host_tag: str,
        ip_set_ids: list[int],
        distribution: str,
        interval_minutes: int,
        managed_pool_id: int | None = None,
    ) -> AutomationGroup:
        group = AutomationGroup(
            org_id=org_id,
            panel_id=panel_id,
            host_tag=host_tag,
            ip_set_ids=ip_set_ids,
            distribution=distribution,
            interval_minutes=interval_minutes,
            managed_pool_id=managed_pool_id,
        )
        self._s.add(group)
        await self._s.commit()
        await self._s.refresh(group)
        return group

    async def toggle_enabled(self, group_id: int, org_id: int) -> AutomationGroup | None:
        group = await self.get_group(group_id, org_id)
        if group:
            group.enabled = not group.enabled
            await self._s.commit()
        return group

    async def delete_group(self, group_id: int, org_id: int) -> bool:
        group = await self.get_group(group_id, org_id)
        if not group:
            return False
        await self._s.delete(group)
        await self._s.commit()
        return True

    async def mark_checked(self, group_id: int) -> None:
        """Set last_checked_at = now (called at the start of each check run)."""
        await self._s.execute(
            update(AutomationGroup)
            .where(AutomationGroup.id == group_id)
            .values(last_checked_at=func.now())
        )
        await self._s.commit()
