from aiogram import F, Router
from aiogram.types import CallbackQuery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.organization import Organization

from services.monitoring_service import MUTE_TTL, _rk_mute_down, _rk_mute_drop
from services.remnawave_api_service import RemnaWaveAPIError, disable_node
from services.remnawave_service import RemnaWaveService

router = Router()


@router.callback_query(F.data.regexp(r"^mon:mute_down:\d+:.+$"))
async def mute_node_down(call: CallbackQuery, redis: Redis) -> None:
    parts = call.data.split(":", 3)
    panel_id = int(parts[2])
    node_uuid = parts[3]
    await redis.set(_rk_mute_down(panel_id, node_uuid), "1", ex=MUTE_TTL)
    await call.answer("🔕 Уведомления о недоступности ноды заглушены на 3 часа.", show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^mon:mute_drop:\d+:.+$"))
async def mute_online_drop(call: CallbackQuery, redis: Redis) -> None:
    parts = call.data.split(":", 3)
    panel_id = int(parts[2])
    node_uuid = parts[3]
    await redis.set(_rk_mute_drop(panel_id, node_uuid), "1", ex=MUTE_TTL)
    await call.answer("🔕 Уведомления о падении онлайна заглушены на 3 часа.", show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^mon:disable:\d+:.+$"))
async def disable_node_from_alert(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    parts = call.data.split(":", 3)
    panel_id = int(parts[2])
    node_uuid = parts[3]

    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await call.answer("Панель не найдена.", show_alert=True)
        return

    try:
        await disable_node(panel.url, panel.api_token, node_uuid)
    except RemnaWaveAPIError as e:
        await call.answer(f"❌ Ошибка: {e}", show_alert=True)
        return

    await call.answer("⛔ Нода отключена.", show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
