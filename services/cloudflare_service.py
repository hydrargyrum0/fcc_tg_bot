from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.cloudflare_settings import CloudflareSettings


class CloudflareService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, org_id: int) -> CloudflareSettings:
        result = await self.session.execute(
            select(CloudflareSettings).where(CloudflareSettings.org_id == org_id)
        )
        settings = result.scalar_one_or_none()
        if not settings:
            settings = CloudflareSettings(org_id=org_id)
            self.session.add(settings)
            await self.session.commit()
            await self.session.refresh(settings)
        return settings

    async def update_email(self, org_id: int, email: str) -> CloudflareSettings:
        settings = await self.get_or_create(org_id)
        settings.email = email
        await self.session.commit()
        return settings

    async def update_api_key(self, org_id: int, api_key: str) -> CloudflareSettings:
        settings = await self.get_or_create(org_id)
        settings.api_key = api_key
        await self.session.commit()
        return settings
