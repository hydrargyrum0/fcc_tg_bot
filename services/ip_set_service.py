from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.ip_set import IpSet


class IpSetService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_org_sets(self, org_id: int) -> list[IpSet]:
        result = await self._s.execute(
            select(IpSet)
            .where(IpSet.org_id == org_id)
            .order_by(IpSet.created_at)
        )
        return list(result.scalars().all())

    async def get_set_by_id(self, set_id: int, org_id: int) -> IpSet | None:
        result = await self._s.execute(
            select(IpSet).where(IpSet.id == set_id, IpSet.org_id == org_id)
        )
        return result.scalar_one_or_none()

    async def add_set(self, org_id: int, tag: str, addresses: str) -> IpSet:
        obj = IpSet(org_id=org_id, tag=tag, addresses=addresses)
        self._s.add(obj)
        await self._s.commit()
        await self._s.refresh(obj)
        return obj

    async def get_sets_by_ids(self, ids: list[int]) -> list[IpSet]:
        """Fetch multiple sets by ID regardless of org (for background tasks)."""
        if not ids:
            return []
        result = await self._s.execute(
            select(IpSet).where(IpSet.id.in_(ids))
        )
        return list(result.scalars().all())

    async def delete_set(self, set_id: int, org_id: int) -> None:
        await self._s.execute(
            delete(IpSet).where(IpSet.id == set_id, IpSet.org_id == org_id)
        )
        await self._s.commit()
