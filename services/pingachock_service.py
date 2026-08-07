"""DB service for Pingachock settings (one record per org)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.pingachock_settings import PingachockSettings


class PingachockService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_settings(self, org_id: int) -> PingachockSettings | None:
        result = await self._session.execute(
            select(PingachockSettings).where(PingachockSettings.org_id == org_id)
        )
        return result.scalar_one_or_none()

    async def save_settings(self, org_id: int, api_url: str, api_key: str) -> PingachockSettings:
        existing = await self.get_settings(org_id)
        if existing:
            existing.api_url = api_url.rstrip("/")
            existing.api_key = api_key
            await self._session.commit()
            return existing
        settings = PingachockSettings(
            org_id=org_id,
            api_url=api_url.rstrip("/"),
            api_key=api_key,
        )
        self._session.add(settings)
        await self._session.commit()
        await self._session.refresh(settings)
        return settings

    async def update_url(self, org_id: int, api_url: str) -> None:
        settings = await self.get_settings(org_id)
        if settings:
            settings.api_url = api_url.rstrip("/")
            await self._session.commit()

    async def update_key(self, org_id: int, api_key: str) -> None:
        settings = await self.get_settings(org_id)
        if settings:
            settings.api_key = api_key
            await self._session.commit()

    async def delete_settings(self, org_id: int) -> None:
        settings = await self.get_settings(org_id)
        if settings:
            await self._session.delete(settings)
            await self._session.commit()
