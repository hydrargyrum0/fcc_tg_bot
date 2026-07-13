from __future__ import annotations
import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import hosts_cancel_kb, hosts_panels_kb, hosts_tags_kb
from bot.states.hosts import HostsFSM
from db.models.organization import Organization
from services.remnawave_api_service import (
    RemnaWaveAPIError,
    get_hosts,
    update_host_address,
)
from services.remnawave_service import RemnaWaveService

router = Router()


def _extract_tags(hosts: list[dict]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for host in hosts:
        for tag in host.get("tags") or []:
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
    return sorted(tags)


@router.callback_query(F.data == "menu:hosts")
async def hosts_menu(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_org_panels(active_org.id)
    if not panels:
        await call.message.edit_text(
            "❌ Нет настроенных панелей Remnawave.\n\nДобавьте панель в Настройках.",
            reply_markup=hosts_panels_kb([]),
        )
        return
    await state.set_state(HostsFSM.choosing_panel)
    await call.message.edit_text(
        "Выберите панель Remnawave:",
        reply_markup=hosts_panels_kb(panels),
    )


@router.callback_query(HostsFSM.choosing_panel, F.data.regexp(r"^hosts:panel:\d+$"))
async def panel_chosen(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    panel_id = int(call.data.split(":")[2])

    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await call.answer("Панель не найдена.", show_alert=True)
        return

    await call.message.edit_text("⏳ Загружаю хосты...")
    try:
        hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await call.message.edit_text(
            f"❌ Не удалось загрузить хосты:\n{e}",
            reply_markup=hosts_panels_kb(await svc.get_org_panels(active_org.id)),
        )
        return

    tags = _extract_tags(hosts)
    if not tags:
        await call.message.edit_text(
            "⚠️ У хостов этой панели нет тегов.",
            reply_markup=hosts_cancel_kb(),
        )
        return

    await state.update_data(panel_id=panel_id, tags=tags)
    await state.set_state(HostsFSM.choosing_tag)
    await call.message.edit_text(
        f"Найдено тегов: {len(tags)}\n\nВыберите тег:",
        reply_markup=hosts_tags_kb(tags),
    )


@router.callback_query(HostsFSM.choosing_tag, F.data.regexp(r"^hosts:tag:\d+$"))
async def tag_chosen(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    tags: list[str] = data["tags"]

    if idx >= len(tags):
        await call.answer("Тег не найден.", show_alert=True)
        return

    tag = tags[idx]
    await state.update_data(selected_tag=tag)
    await state.set_state(HostsFSM.waiting_address)
    await call.message.edit_text(
        f"Тег: <b>{tag}</b>\n\nВведите IP адрес или домен для всех хостов с этим тегом:",
        reply_markup=hosts_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(HostsFSM.waiting_address)
async def got_address(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    address = (message.text or "").strip()
    if not address:
        await message.answer("Введите IP адрес или домен:", reply_markup=hosts_cancel_kb())
        return

    data = await state.get_data()
    panel_id: int = data["panel_id"]
    tag: str = data["selected_tag"]

    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await state.clear()
        await message.answer("❌ Панель Remnawave не найдена.")
        return

    status_msg = await message.answer(f"⏳ Обновляю хосты с тегом <b>{tag}</b>...", parse_mode="HTML")

    try:
        hosts = await get_hosts(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await state.clear()
        await status_msg.edit_text(f"❌ Не удалось загрузить хосты:\n{e}")
        return

    targets = [h for h in hosts if tag in (h.get("tags") or [])]
    if not targets:
        await state.clear()
        await status_msg.edit_text(f"⚠️ Хосты с тегом <b>{tag}</b> не найдены.", parse_mode="HTML")
        return

    errors: list[str] = []

    async def update_one(host: dict) -> None:
        try:
            await update_host_address(panel.url, panel.api_token, host["uuid"], address)
        except RemnaWaveAPIError as e:
            errors.append(f"• {host.get('remark', host['uuid'])}: {str(e)[:60]}")

    await asyncio.gather(*[update_one(h) for h in targets])
    await state.clear()

    if errors:
        err_text = "\n".join(errors)
        await status_msg.edit_text(
            f"⚠️ Обновлено {len(targets) - len(errors)} из {len(targets)} хостов.\n\n"
            f"Ошибки:\n{err_text}",
            parse_mode="HTML",
        )
    else:
        await status_msg.edit_text(
            f"✅ Готово!\n\n"
            f"🏷 Тег: <b>{tag}</b>\n"
            f"🌐 Адрес: <code>{address}</code>\n"
            f"📡 Обновлено хостов: {len(targets)}",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "hosts:cancel")
async def cancel_hosts(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    from bot.keyboards.inline import main_menu_kb
    await call.message.edit_text("Главное меню:", reply_markup=main_menu_kb())
