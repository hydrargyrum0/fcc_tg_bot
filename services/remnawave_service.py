from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.remnawave_panel import RemnaWavePanel


class RemnaWaveService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_user_panels(self, user_id: int) -> list[RemnaWavePanel]:
        result = await self.session.execute(
            select(RemnaWavePanel)
            .where(RemnaWavePanel.user_id == user_id)
            .order_by(RemnaWavePanel.created_at)
        )
        return list(result.scalars().all())

    async def add_panel(self, user_id: int, url: str, api_token: str, tag: str) -> RemnaWavePanel:
        panel = RemnaWavePanel(user_id=user_id, url=url, api_token=api_token, tag=tag)
        self.session.add(panel)
        await self.session.commit()
        await self.session.refresh(panel)
        return panel

    async def get_panel_by_id(self, panel_id: int, user_id: int) -> RemnaWavePanel | None:
        result = await self.session.execute(
            select(RemnaWavePanel).where(
                RemnaWavePanel.id == panel_id,
                RemnaWavePanel.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def update_url(self, panel_id: int, user_id: int, url: str) -> bool:
        panel = await self.get_panel_by_id(panel_id, user_id)
        if not panel:
            return False
        panel.url = url
        await self.session.commit()
        return True

    async def update_tag(self, panel_id: int, user_id: int, tag: str) -> bool:
        panel = await self.get_panel_by_id(panel_id, user_id)
        if not panel:
            return False
        panel.tag = tag
        await self.session.commit()
        return True

    async def update_token(self, panel_id: int, user_id: int, api_token: str) -> bool:
        panel = await self.get_panel_by_id(panel_id, user_id)
        if not panel:
            return False
        panel.api_token = api_token
        await self.session.commit()
        return True

    async def delete_panel(self, panel_id: int, user_id: int) -> bool:
        panel = await self.get_panel_by_id(panel_id, user_id)
        if not panel:
            return False
        await self.session.delete(panel)
        await self.session.commit()
        return True
