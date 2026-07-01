import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis

from db.session import async_session_factory
from services.organization_service import OrganizationService
from services.remnawave_api_service import RemnaWaveAPIError, get_nodes
from services.remnawave_service import RemnaWaveService

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60

ONLINE_DROP_RATIO = 0.30
ONLINE_DROP_MIN_PREV = 10

MUTE_TTL = 3 * 60 * 60


def _rk_down(panel_id: int, uuid: str) -> str:
    return f"mon:down:{panel_id}:{uuid}"

def _rk_mute_down(panel_id: int, uuid: str) -> str:
    return f"mon:mute_down:{panel_id}:{uuid}"

def _rk_drop(panel_id: int, uuid: str) -> str:
    return f"mon:drop:{panel_id}:{uuid}"

def _rk_mute_drop(panel_id: int, uuid: str) -> str:
    return f"mon:mute_drop:{panel_id}:{uuid}"

def _rk_prev_online(panel_id: int, uuid: str) -> str:
    return f"mon:prev_online:{panel_id}:{uuid}"


def _node_offline_kb(panel_id: int, node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔕 Заглушить на 3 часа", callback_data=f"mon:mute_down:{panel_id}:{node_uuid}")],
        [InlineKeyboardButton(text="⛔ Отключить ноду", callback_data=f"mon:disable:{panel_id}:{node_uuid}")],
    ])


def _online_drop_kb(panel_id: int, node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔕 Заглушить на 3 часа", callback_data=f"mon:mute_drop:{panel_id}:{node_uuid}")],
    ])


async def _get_panel_org_member_ids(panel_id: int, org_id: int | None) -> list[int]:
    if not org_id:
        return []
    async with async_session_factory() as session:
        org_svc = OrganizationService(session)
        return await org_svc.get_active_member_user_ids(org_id)


async def _broadcast(bot: Bot, member_ids: list[int], text: str, kb=None) -> None:
    for uid in member_ids:
        try:
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            logger.debug("Monitoring: send to %d failed: %s", uid, e)


async def _check_panel(bot: Bot, redis: Redis, panel) -> None:
    try:
        nodes = await get_nodes(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        logger.warning("Monitoring: failed to fetch nodes for panel %d: %s", panel.id, e)
        return

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    member_ids: list[int] | None = None

    async def _ensure_members() -> list[int]:
        nonlocal member_ids
        if member_ids is None:
            member_ids = await _get_panel_org_member_ids(panel.id, panel.org_id)
        return member_ids

    for node in nodes:
        uuid = node["uuid"]
        name = node.get("name", uuid)
        address = node.get("address") or "—"
        port = node.get("port")
        addr_str = f"{address}:{port}" if port else address
        users_online = node.get("usersOnline", 0)

        is_disabled = node.get("isDisabled", False)
        is_connected = node.get("isConnected", False)

        # ── Offline check ──────────────────────────────────────────────────
        if not is_disabled:
            down_key = _rk_down(panel.id, uuid)
            mute_key = _rk_mute_down(panel.id, uuid)

            if not is_connected:
                already_alerted = await redis.exists(down_key)
                is_muted = await redis.exists(mute_key)
                if not already_alerted and not is_muted:
                    last_msg = node.get("lastStatusMessage") or "—"
                    text = (
                        f"🔴 Панель {panel.tag} — нода {name}, {addr_str}\n"
                        f"Обнаружена ошибка, узел недоступен.\n\n"
                        f"🕐 {now_str}\n"
                        f"Сообщение: {last_msg}"
                    )
                    await _broadcast(bot, await _ensure_members(), text, _node_offline_kb(panel.id, uuid))
                    await redis.set(down_key, "1")
            else:
                await redis.delete(down_key)

        # ── Online drop check ──────────────────────────────────────────────
        prev_key = _rk_prev_online(panel.id, uuid)
        drop_key = _rk_drop(panel.id, uuid)
        mute_drop_key = _rk_mute_drop(panel.id, uuid)

        prev_raw = await redis.get(prev_key)
        if prev_raw is not None:
            prev_online = int(prev_raw)
            if (
                prev_online >= ONLINE_DROP_MIN_PREV
                and users_online < prev_online * ONLINE_DROP_RATIO
            ):
                already_drop_alerted = await redis.exists(drop_key)
                is_muted = await redis.exists(mute_drop_key)
                if not already_drop_alerted and not is_muted:
                    text = (
                        f"⚠️ Панель {panel.tag} — нода {name}, {addr_str}\n"
                        f"Внимание!\n"
                        f"Обнаружено резкое падение онлайна {prev_online}→{users_online}\n\n"
                        f"🕐 {now_str}"
                    )
                    await _broadcast(bot, await _ensure_members(), text, _online_drop_kb(panel.id, uuid))
                    await redis.set(drop_key, "1", ex=MUTE_TTL)
            else:
                await redis.delete(drop_key)

        await redis.set(prev_key, str(users_online))


async def run_monitoring(bot: Bot, redis: Redis) -> None:
    logger.info("Monitoring service started (interval=%ds)", CHECK_INTERVAL)
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            async with async_session_factory() as session:
                svc = RemnaWaveService(session)
                panels = await svc.get_all_monitored_panels()

            await asyncio.gather(*[_check_panel(bot, redis, p) for p in panels])
        except Exception as e:
            logger.error("Monitoring loop error: %s", e)
