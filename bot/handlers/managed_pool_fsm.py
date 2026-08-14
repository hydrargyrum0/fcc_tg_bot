"""Handlers for Managed IP Pool (Модерируемые пулы).

UI flow:
  menu:ip_sets → mpool:list → mpool:detail:{id}
                             → mpool:scan:{id}      (trigger manual re-scan)
                             → mpool:delete_confirm → mpool:delete

  mpool:add → ManagedPoolFSM:
    choosing_name
    → choosing_ip_sets  (multi-select from org IP sets)
    → choosing_tag      (pick Remnawave host tag)
    → choosing_threshold (score threshold 0–100, default 60)
    → choosing_interval  (re-scan interval in minutes, default 120)
    → confirming
    → mpool:confirm_create → save → back to list
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    mpool_cancel_kb,
    mpool_confirm_kb,
    mpool_delete_confirm_kb,
    mpool_detail_kb,
    mpool_ip_sets_kb,
    mpool_list_kb,
    mpool_tags_kb,
)
from bot.states.managed_pool import ManagedPoolFSM
from db.models.managed_pool import ManagedPool
from db.models.organization import Organization
from db.models.user import User
from services.audit_service import send_audit
from services.ip_set_service import IpSetService
from services.managed_pool_service import ManagedPoolService
from services.remnawave_api_service import RemnaWaveAPIError, get_hosts
from services.remnawave_service import RemnaWaveService

router = Router()


def _extract_unique_tags(hosts: list[dict]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for host in hosts:
        for tag in host.get("tags") or []:
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
    return sorted(tags)


async def _show_pool_list(
    call: CallbackQuery, session: AsyncSession, active_org: Organization,
) -> None:
    svc = ManagedPoolService(session)
    pools = await svc.get_org_pools(active_org.id)
    if pools:
        lines = "\n".join(f"• {p.name}" for p in pools)
        text = f"⚙️ <b>Модерируемые пулы</b>\n\n{lines}"
    else:
        text = "⚙️ <b>Модерируемые пулы</b>\n\nПулов пока нет. Создайте первый!"
    await call.message.edit_text(text, reply_markup=mpool_list_kb(pools), parse_mode="HTML")


# ── list ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "mpool:list")
async def mpool_list(
    call: CallbackQuery, session: AsyncSession, active_org: Organization,
) -> None:
    await call.answer()
    await _show_pool_list(call, session, active_org)


# ── detail ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^mpool:detail:\d+$"))
async def mpool_detail(
    call: CallbackQuery, session: AsyncSession, active_org: Organization,
) -> None:
    await call.answer()
    pool_id = int(call.data.split(":")[2])
    svc = ManagedPoolService(session)
    pool = await svc.get_pool(pool_id, active_org.id)
    if not pool:
        await call.answer("Пул не найден.", show_alert=True)
        return

    stats = await svc.get_pool_stats(pool_id)
    ip_svc = IpSetService(session)
    sets = await ip_svc.get_sets_by_ids(list(pool.ip_set_ids))
    sets_label = ", ".join(s.tag for s in sets) if sets else "—"

    last = "ещё не проверялся"
    if pool.last_scanned_at:
        from datetime import timezone as _tz
        ts = pool.last_scanned_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        last = ts.strftime("%Y-%m-%d %H:%M UTC")

    total = stats["total"]
    approved = stats["approved"]
    pct = f"{approved / total * 100:.0f}%" if total else "—"

    from aiogram.exceptions import TelegramBadRequest
    try:
        await call.message.edit_text(
            f"⚙️ <b>Пул «{pool.name}»</b>\n\n"
            f"Источники: <b>{sets_label}</b>\n"
            f"Тег хостов: <b>{pool.host_tag}</b>\n"
            f"Порог: {pool.score_threshold:.0f} | Интервал: {pool.check_interval_minutes} мин\n\n"
            f"📊 Всего: {total} | Одобрено: {approved} ({pct}) | "
            f"Ожидают: {stats['pending']} | Отклонено: {stats['rejected']}\n\n"
            f"Последняя проверка: {last}",
            reply_markup=mpool_detail_kb(pool_id),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ── manual scan trigger ───────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^mpool:scan:\d+$"))
async def mpool_scan(
    call: CallbackQuery, session: AsyncSession, active_org: Organization,
) -> None:
    pool_id = int(call.data.split(":")[2])
    svc = ManagedPoolService(session)
    pool = await svc.get_pool(pool_id, active_org.id)
    if not pool:
        await call.answer("Пул не найден.", show_alert=True)
        return
    # Clear last_scanned_at so the scorer picks it up on the next cycle
    await svc.reset_last_scanned(pool_id)
    await call.answer(
        "🔄 Проверка будет запущена в течение минуты. Результаты появятся через несколько минут.",
        show_alert=True,
    )


# ── delete ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^mpool:delete_confirm:\d+$"))
async def mpool_delete_confirm(
    call: CallbackQuery, session: AsyncSession, active_org: Organization,
) -> None:
    await call.answer()
    pool_id = int(call.data.split(":")[2])
    svc = ManagedPoolService(session)
    pool = await svc.get_pool(pool_id, active_org.id)
    if not pool:
        await call.answer("Пул не найден.", show_alert=True)
        return
    await call.message.edit_text(
        f"🗑 Удалить пул <b>«{pool.name}»</b>?\n\nВсе оценённые IP будут удалены.",
        reply_markup=mpool_delete_confirm_kb(pool_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^mpool:delete:\d+$"))
async def mpool_delete(
    call: CallbackQuery,
    session: AsyncSession,
    active_org: Organization,
    db_user: User,
) -> None:
    await call.answer()
    pool_id = int(call.data.split(":")[2])
    svc = ManagedPoolService(session)
    pool = await svc.get_pool(pool_id, active_org.id)
    if not pool:
        await call.answer("Пул не найден.", show_alert=True)
        return
    name = pool.name
    await svc.delete_pool(pool_id, active_org.id)
    send_audit(call.bot, active_org.id, db_user, f"Удалил управляемый пул: {name}")
    await _show_pool_list(call, session, active_org)


# ── FSM: create new pool ──────────────────────────────────────────────────────

@router.callback_query(F.data == "mpool:add")
async def mpool_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(ManagedPoolFSM.choosing_name)
    await call.message.edit_text(
        "➕ <b>Новый модерируемый пул</b>\n\nВведите название пула:",
        reply_markup=mpool_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(ManagedPoolFSM.choosing_name)
async def mpool_got_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 200:
        await message.answer(
            "Название должно быть от 1 до 200 символов:",
            reply_markup=mpool_cancel_kb(),
        )
        return

    ip_svc = IpSetService(session)
    sets = await ip_svc.get_org_sets(active_org.id)
    if not sets:
        await message.answer(
            "❌ Нет пользовательских наборов IP.\n\n"
            "Сначала добавьте набор в «Пользовательские списки».",
            reply_markup=mpool_cancel_kb(),
        )
        return

    sets_info = [{"id": s.id, "tag": s.tag} for s in sets]
    await state.update_data(name=name, sets_info=sets_info, selected_set_ids=[])
    await state.set_state(ManagedPoolFSM.choosing_ip_sets)

    class _S:
        def __init__(self, d: dict) -> None:
            self.id = d["id"]
            self.tag = d["tag"]

    await message.answer(
        f"Пул: <b>{name}</b>\n\nВыберите пользовательские наборы IP для этого пула:",
        reply_markup=mpool_ip_sets_kb([_S(s) for s in sets_info], []),
        parse_mode="HTML",
    )


@router.callback_query(ManagedPoolFSM.choosing_ip_sets, F.data.regexp(r"^mpool:toggle_set:\d+$"))
async def mpool_toggle_set(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    set_id = int(call.data.split(":")[2])
    data = await state.get_data()
    selected: list[int] = list(data.get("selected_set_ids", []))
    if set_id in selected:
        selected.remove(set_id)
    else:
        selected.append(set_id)
    await state.update_data(selected_set_ids=selected)

    sets_info: list[dict] = data["sets_info"]

    class _S:
        def __init__(self, d: dict) -> None:
            self.id = d["id"]
            self.tag = d["tag"]

    await call.message.edit_reply_markup(
        reply_markup=mpool_ip_sets_kb([_S(s) for s in sets_info], selected)
    )


@router.callback_query(ManagedPoolFSM.choosing_ip_sets, F.data == "mpool:confirm_sets")
async def mpool_confirm_sets(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    data = await state.get_data()
    selected: list[int] = data.get("selected_set_ids", [])
    if not selected:
        await call.answer("Выберите хотя бы один набор.", show_alert=True)
        return

    rw_svc = RemnaWaveService(session)
    panels = await rw_svc.get_org_panels(active_org.id)
    all_tags: set[str] = set()
    for panel in panels:
        try:
            hosts = await get_hosts(panel.url, panel.api_token)
            all_tags.update(_extract_unique_tags(hosts))
        except RemnaWaveAPIError:
            pass
    tags = sorted(all_tags)

    if not tags:
        await call.answer(
            "Нет доступных тегов хостов. Проверьте подключение к Remnawave.",
            show_alert=True,
        )
        return

    await state.update_data(tags=tags)
    await state.set_state(ManagedPoolFSM.choosing_tag)
    await call.message.edit_text(
        "Выберите тег хостов Remnawave для этого пула:",
        reply_markup=mpool_tags_kb(tags),
    )


@router.callback_query(ManagedPoolFSM.choosing_tag, F.data.regexp(r"^mpool:tag:\d+$"))
async def mpool_tag_chosen(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    tags: list[str] = data["tags"]
    if idx >= len(tags):
        await call.answer("Тег не найден.", show_alert=True)
        return
    tag = tags[idx]
    await state.update_data(host_tag=tag)
    await state.set_state(ManagedPoolFSM.choosing_threshold)
    await call.message.edit_text(
        f"Тег: <b>{tag}</b>\n\n"
        "Минимальный балл для одобрения IP (0–100).\n"
        "Рекомендуется: <b>60</b> — адреса с рабочим TLS и низкими потерями пакетов.\n\n"
        "Введите число или /skip для значения по умолчанию (60):",
        reply_markup=mpool_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(ManagedPoolFSM.choosing_threshold)
async def mpool_got_threshold(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == "/skip":
        threshold = 60.0
    else:
        try:
            threshold = float(text)
            if not (0 <= threshold <= 100):
                raise ValueError
        except ValueError:
            await message.answer(
                "Введите число от 0 до 100 (или /skip для 60):",
                reply_markup=mpool_cancel_kb(),
            )
            return
    await state.update_data(threshold=threshold)
    await state.set_state(ManagedPoolFSM.choosing_interval)
    await message.answer(
        f"Порог: <b>{threshold:.0f}</b>\n\n"
        "Интервал пересканирования пула (в минутах, минимум 30).\n"
        "Введите число или /skip для 120 мин:",
        reply_markup=mpool_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(ManagedPoolFSM.choosing_interval)
async def mpool_got_interval(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == "/skip":
        interval = 120
    else:
        try:
            interval = int(text)
            if interval < 30:
                raise ValueError
        except ValueError:
            await message.answer(
                "Введите целое число ≥ 30 (или /skip для 120):",
                reply_markup=mpool_cancel_kb(),
            )
            return
    await state.update_data(interval=interval)
    await state.set_state(ManagedPoolFSM.confirming)
    await _show_pool_confirm(message, state)


async def _show_pool_confirm(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    name = data.get("name", "?")
    host_tag = data.get("host_tag", "?")
    sets_info: list[dict] = data.get("sets_info", [])
    selected_ids: list[int] = data.get("selected_set_ids", [])
    threshold: float = data.get("threshold", 60.0)
    interval: int = data.get("interval", 120)

    selected_sets = [s for s in sets_info if s["id"] in selected_ids]
    sets_label = ", ".join(s["tag"] for s in selected_sets) or "—"

    text = (
        f"✅ <b>Готово к созданию</b>\n\n"
        f"Название: <b>{name}</b>\n"
        f"Наборы IP: <b>{sets_label}</b>\n"
        f"Тег хостов: <b>{host_tag}</b>\n"
        f"Порог одобрения: <b>{threshold:.0f}</b>\n"
        f"Интервал проверки: <b>{interval} мин</b>"
    )
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer(text, reply_markup=mpool_confirm_kb(), parse_mode="HTML")


@router.callback_query(ManagedPoolFSM.confirming, F.data == "mpool:confirm_create")
async def mpool_confirm_create(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
    db_user: User,
) -> None:
    await call.answer()
    data = await state.get_data()
    svc = ManagedPoolService(session)
    pool = await svc.create_pool(
        org_id=active_org.id,
        name=data["name"],
        host_tag=data["host_tag"],
        ip_set_ids=data.get("selected_set_ids", []),
        score_threshold=data.get("threshold", 60.0),
        check_interval_minutes=data.get("interval", 120),
    )
    await state.clear()
    send_audit(call.bot, active_org.id, db_user, f"Создал управляемый пул: {pool.name}")
    await _show_pool_list(call, session, active_org)


# ── FSM back navigation ───────────────────────────────────────────────────────

@router.callback_query(F.data == "mpool:back")
async def mpool_back(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    active_org: Organization,
) -> None:
    await call.answer()
    current = await state.get_state()
    data = await state.get_data()

    if current is None or current == ManagedPoolFSM.choosing_name:
        await state.clear()
        await _show_pool_list(call, session, active_org)

    elif current == ManagedPoolFSM.choosing_ip_sets:
        await state.set_state(ManagedPoolFSM.choosing_name)
        await call.message.edit_text(
            "➕ <b>Новый модерируемый пул</b>\n\nВведите название пула:",
            reply_markup=mpool_cancel_kb(),
            parse_mode="HTML",
        )

    elif current == ManagedPoolFSM.choosing_tag:
        sets_info = data.get("sets_info", [])
        selected = data.get("selected_set_ids", [])

        class _S:
            def __init__(self, d: dict) -> None:
                self.id = d["id"]
                self.tag = d["tag"]

        await state.set_state(ManagedPoolFSM.choosing_ip_sets)
        await call.message.edit_text(
            "Выберите пользовательские наборы IP:",
            reply_markup=mpool_ip_sets_kb([_S(s) for s in sets_info], selected),
        )

    elif current == ManagedPoolFSM.choosing_threshold:
        tags = data.get("tags", [])
        await state.set_state(ManagedPoolFSM.choosing_tag)
        await call.message.edit_text(
            "Выберите тег хостов Remnawave для этого пула:",
            reply_markup=mpool_tags_kb(tags),
        )

    elif current == ManagedPoolFSM.choosing_interval:
        threshold = data.get("threshold", 60.0)
        await state.set_state(ManagedPoolFSM.choosing_threshold)
        await call.message.edit_text(
            f"Текущий порог: {threshold:.0f}\n\nВведите минимальный балл (0–100) или /skip для 60:",
            reply_markup=mpool_cancel_kb(),
        )

    elif current == ManagedPoolFSM.confirming:
        interval = data.get("interval", 120)
        await state.set_state(ManagedPoolFSM.choosing_interval)
        await call.message.edit_text(
            f"Текущий интервал: {interval} мин\n\nВведите число ≥ 30 или /skip для 120:",
            reply_markup=mpool_cancel_kb(),
        )

    else:
        await state.clear()
        await _show_pool_list(call, session, active_org)
