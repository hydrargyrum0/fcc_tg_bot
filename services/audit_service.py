import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot

from db.models.user import User
from db.session import async_session_factory
from services.organization_service import OrganizationService

logger = logging.getLogger(__name__)


async def _send_audit(bot: Bot, org_id: int, actor: User, action: str) -> None:
    actor_label = f"@{actor.username}" if actor.username else actor.full_name
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
    text = f"📋 Аудит\n{actor_label} ({actor.id})\n{action}\n🕐 {now}"

    async with async_session_factory() as session:
        org_svc = OrganizationService(session)
        member_ids = await org_svc.get_active_member_user_ids(org_id)

    recipients = [uid for uid in member_ids if uid != actor.id]

    async def _send_one(uid: int) -> None:
        try:
            await bot.send_message(uid, text)
        except Exception as e:
            logger.debug("Audit: failed to send to %d: %s", uid, e)

    await asyncio.gather(*[_send_one(uid) for uid in recipients])


def send_audit(bot: Bot, org_id: int, actor: User, action: str) -> None:
    """Fire-and-forget audit notification. Call without await."""
    asyncio.create_task(_send_audit(bot, org_id, actor, action))
