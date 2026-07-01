from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.remnawave_panel import RemnaWavePanel


class RemnaWaveService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_org_panels(self, org_id: int) -> list[RemnaWavePanel]:
        result = await self.session.execute(
            select(RemnaWavePanel)
            .where(RemnaWavePanel.org_id == org_id)
            .order_by(RemnaWavePanel.created_at)
        )
        return list(result.scalars().all())

    async def add_panel(
        self,
        org_id: int,
        url: str,
        api_token: str,
        node_secret_key: str,
        node_port: int,
        tag: str,
    ) -> RemnaWavePanel:
        panel = RemnaWavePanel(
            org_id=org_id,
            url=url,
            api_token=api_token,
            node_secret_key=node_secret_key,
            node_port=node_port,
            tag=tag,
        )
        self.session.add(panel)
        await self.session.commit()
        await self.session.refresh(panel)
        return panel

    async def get_panel_by_id(self, panel_id: int, org_id: int) -> RemnaWavePanel | None:
        result = await self.session.execute(
            select(RemnaWavePanel).where(
                RemnaWavePanel.id == panel_id,
                RemnaWavePanel.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def update_url(self, panel_id: int, org_id: int, url: str) -> bool:
        panel = await self.get_panel_by_id(panel_id, org_id)
        if not panel:
            return False
        panel.url = url
        await self.session.commit()
        return True

    async def update_tag(self, panel_id: int, org_id: int, tag: str) -> bool:
        panel = await self.get_panel_by_id(panel_id, org_id)
        if not panel:
            return False
        panel.tag = tag
        await self.session.commit()
        return True

    async def update_token(self, panel_id: int, org_id: int, api_token: str) -> bool:
        panel = await self.get_panel_by_id(panel_id, org_id)
        if not panel:
            return False
        panel.api_token = api_token
        await self.session.commit()
        return True

    async def update_node_secret(self, panel_id: int, org_id: int, node_secret_key: str) -> bool:
        panel = await self.get_panel_by_id(panel_id, org_id)
        if not panel:
            return False
        panel.node_secret_key = node_secret_key
        await self.session.commit()
        return True

    async def update_node_port(self, panel_id: int, org_id: int, node_port: int) -> bool:
        panel = await self.get_panel_by_id(panel_id, org_id)
        if not panel:
            return False
        panel.node_port = node_port
        await self.session.commit()
        return True

    async def toggle_monitoring(self, panel_id: int, org_id: int) -> bool | None:
        panel = await self.get_panel_by_id(panel_id, org_id)
        if not panel:
            return None
        panel.monitoring_enabled = not panel.monitoring_enabled
        await self.session.commit()
        return panel.monitoring_enabled

    async def get_all_monitored_panels(self) -> list[RemnaWavePanel]:
        result = await self.session.execute(
            select(RemnaWavePanel).where(RemnaWavePanel.monitoring_enabled.is_(True))
        )
        return list(result.scalars().all())

    async def delete_panel(self, panel_id: int, org_id: int) -> bool:
        panel = await self.get_panel_by_id(panel_id, org_id)
        if not panel:
            return False
        await self.session.delete(panel)
        await self.session.commit()
        return True
