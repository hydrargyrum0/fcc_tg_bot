from __future__ import annotations
import asyncio
import base64
import copy
import ipaddress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    config_profile_select_kb,
    deploy_cancel_kb,
    deploy_presets_kb,
    deploy_select_panel_kb,
)
from bot.states.deploy import DeployHysteria, DeployRemnanode
from db.models.organization import Organization
from services.cloudflare_api import (
    CloudflareAPIError,
    create_a_record,
    find_zone,
    get_a_record,
    get_zone,
    list_zones,
    update_a_record,
)
from services.cloudflare_service import CloudflareService
from services.remnawave_api_service import (
    RemnaWaveAPIError,
    create_node,
    get_config_profile,
    get_config_profiles,
    get_profile_inbounds,
    get_profile_inbounds_detailed,
    update_config_profile,
)
from services.remnawave_service import RemnaWaveService
from services.ssh_certbot_service import deploy_remnanode_hysteria
from services.ssh_deploy_service import (
    SSHAuthError,
    SSHKeyPassphraseRequired,
    deploy_remnanode,
    parse_private_key,
)

router = Router()

_DEPLOY_TEXT = "Выберите пресет для развёртывания на вашем VPS:"


@router.callback_query(F.data == "menu:deploy")
async def deploy_menu(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(_DEPLOY_TEXT, reply_markup=deploy_presets_kb())



@router.callback_query(F.data == "deploy:cancel")
async def cancel_deploy(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await call.message.edit_text(_DEPLOY_TEXT, reply_markup=deploy_presets_kb())


# ─────────────────────────────────────────────
#  STABLE REMNANODE PRESET
# ─────────────────────────────────────────────

@router.callback_query(F.data == "deploy:preset:remnanode_stable")
async def preset_remnanode_stable(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = RemnaWaveService(session)
    panels = await svc.get_org_panels(active_org.id)
    await state.set_state(DeployRemnanode.choosing_panel)
    await call.message.edit_text(
        "К какому Remnawave подключить ноду?",
        reply_markup=deploy_select_panel_kb(panels),
    )


@router.callback_query(
    DeployRemnanode.choosing_panel,
    F.data.regexp(r"^deploy:panel:(none|\d+)$"),
)
async def panel_chosen(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    value = call.data.split(":")[2]

    if value == "none":
        panel_id = None
    else:
        panel_id = int(value)
        svc = RemnaWaveService(session)
        panel = await svc.get_panel_by_id(panel_id, active_org.id)
        if not panel:
            await call.answer("Панель не найдена.", show_alert=True)
            return

    await state.update_data(panel_id=panel_id)
    await state.set_state(DeployRemnanode.waiting_ip)
    await call.message.edit_text(
        "Укажите IP адреса серверов — каждый с новой строки:",
        reply_markup=deploy_cancel_kb(),
    )


@router.message(DeployRemnanode.waiting_ip)
async def got_ip(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    if not lines:
        await message.answer(
            "Укажите хотя бы один IP адрес (каждый с новой строки):",
            reply_markup=deploy_cancel_kb(),
        )
        return

    invalid = []
    valid = []
    for line in lines:
        try:
            ipaddress.ip_address(line)
            valid.append(line)
        except ValueError:
            invalid.append(line)

    if invalid:
        await message.answer(
            f"Некорректные IP адреса:\n" + "\n".join(invalid) +
            "\n\nПопробуйте ещё раз:",
            reply_markup=deploy_cancel_kb(),
        )
        return

    await state.update_data(ips=valid)
    await state.set_state(DeployRemnanode.waiting_login)

    ips_text = "\n".join(f"• {ip}" for ip in valid)
    count = len(valid)
    await message.answer(
        f"{'IP адрес' if count == 1 else f'IP адреса ({count}шт.)'}:\n{ips_text}\n\n"
        "Укажите логин SSH-пользователя:",
        reply_markup=deploy_cancel_kb(),
    )


@router.message(DeployRemnanode.waiting_login)
async def got_login(message: Message, state: FSMContext) -> None:
    login = (message.text or "").strip()
    if not login:
        await message.answer("Логин не может быть пустым. Попробуйте ещё раз:", reply_markup=deploy_cancel_kb())
        return

    await state.update_data(login=login)
    await state.set_state(DeployRemnanode.waiting_auth)
    await message.answer(
        "Отправьте пароль от сервера текстом, либо прикрепите файл с приватным SSH-ключом:",
        reply_markup=deploy_cancel_kb(),
    )


@router.message(DeployRemnanode.waiting_auth, F.document)
async def auth_got_key_file(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    file = await message.bot.get_file(message.document.file_id)
    file_io = await message.bot.download_file(file.file_path)
    key_bytes = file_io.read()

    try:
        parse_private_key(key_bytes)
    except SSHKeyPassphraseRequired:
        await state.update_data(
            auth_method="key",
            key_data_b64=base64.b64encode(key_bytes).decode(),
        )
        await state.set_state(DeployRemnanode.waiting_key_passphrase)
        await message.answer(
            "Этот ключ защищён key-phrase. Укажите key-phrase от ключа:",
            reply_markup=deploy_cancel_kb(),
        )
        return
    except SSHAuthError as e:
        await message.answer(f"{e}\nПришлите корректный файл с ключом:", reply_markup=deploy_cancel_kb())
        return

    await state.update_data(
        auth_method="key",
        key_data_b64=base64.b64encode(key_bytes).decode(),
        key_passphrase=None,
    )
    await _after_auth(message, state, active_org, session)


@router.message(DeployRemnanode.waiting_auth)
async def auth_got_password(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    password = (message.text or "").strip()
    if not password:
        await message.answer(
            "Пришлите пароль текстом или файл с приватным ключом:",
            reply_markup=deploy_cancel_kb(),
        )
        return

    await state.update_data(auth_method="password", password=password)
    await _after_auth(message, state, active_org, session)


@router.message(DeployRemnanode.waiting_key_passphrase)
async def auth_got_passphrase(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    passphrase = (message.text or "").strip()
    data = await state.get_data()
    key_bytes = base64.b64decode(data["key_data_b64"])

    try:
        parse_private_key(key_bytes, passphrase=passphrase)
    except SSHAuthError as e:
        await message.answer(f"{e}\nВведите key-phrase ещё раз:", reply_markup=deploy_cancel_kb())
        return

    await state.update_data(key_passphrase=passphrase)
    await _after_auth(message, state, active_org, session)


async def _after_auth(message: Message, state: FSMContext, active_org: Organization, session: AsyncSession) -> None:
    data = await state.get_data()
    if data.get("panel_id") is None:
        await state.set_state(DeployRemnanode.waiting_secret_key)
        await message.answer("Укажите Remnanode Secret Key:", reply_markup=deploy_cancel_kb())
    else:
        await _run_deployment(message, state, active_org, session)


@router.message(DeployRemnanode.waiting_secret_key)
async def got_secret_key(message: Message, state: FSMContext) -> None:
    secret_key = (message.text or "").strip()
    if not secret_key:
        await message.answer("Secret Key не может быть пустым. Попробуйте ещё раз:", reply_markup=deploy_cancel_kb())
        return
    await state.update_data(secret_key=secret_key)
    await state.set_state(DeployRemnanode.waiting_port)
    await message.answer("Укажите порт для Remnanode:", reply_markup=deploy_cancel_kb())


@router.message(DeployRemnanode.waiting_port)
async def got_port(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Порт должен быть числом. Попробуйте ещё раз:", reply_markup=deploy_cancel_kb())
        return
    await state.update_data(node_port=int(text))
    await _run_deployment(message, state, active_org, session)


async def _run_deployment(message: Message, state: FSMContext, active_org: Organization, session: AsyncSession) -> None:
    data = await state.get_data()
    panel_id = data.get("panel_id")
    ips: list[str] = data["ips"]
    n = len(ips)

    if panel_id is not None:
        svc = RemnaWaveService(session)
        panel = await svc.get_panel_by_id(panel_id, active_org.id)
        if not panel:
            await message.answer("Выбранная панель Remnawave не найдена.")
            await state.clear()
            return
        secret_key = panel.node_secret_key
        node_port = panel.node_port
    else:
        panel = None
        secret_key = data["secret_key"]
        node_port = data["node_port"]

    password = data.get("password")
    client_key = None
    if data.get("auth_method") == "key":
        key_bytes = base64.b64decode(data["key_data_b64"])
        client_key = parse_private_key(key_bytes, passphrase=data.get("key_passphrase"))

    # Per-IP status tracking with background updater
    ip_statuses: dict[str, str] = {ip: "⏳ Подключаюсь..." for ip in ips}

    def _build_status_text() -> str:
        lines = [f"• {ip} — {status}" for ip, status in ip_statuses.items()]
        return "🚀 Развёртываю ноды...\n\n" + "\n".join(lines)

    status_msg = await message.answer(_build_status_text())

    _last_sent: list[str] = [""]

    async def _periodic_updater() -> None:
        while True:
            await asyncio.sleep(5)
            text = _build_status_text()
            if text == _last_sent[0]:
                continue
            _last_sent[0] = text
            try:
                await asyncio.wait_for(status_msg.edit_text(text), timeout=8)
            except asyncio.TimeoutError:
                pass
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramBadRequest:
                pass
            except Exception:
                pass

    async def deploy_one(ip: str) -> tuple[str, str | None]:
        async def progress(text: str) -> None:
            ip_statuses[ip] = text

        try:
            await deploy_remnanode(
                ip=ip,
                login=data["login"],
                password=password,
                client_key=client_key,
                secret_key=secret_key,
                port=node_port,
                progress_cb=progress,
            )
            ip_statuses[ip] = "✅ Установлена"
            return ip, None
        except Exception as e:
            ip_statuses[ip] = f"❌ {str(e)[:60]}"
            return ip, str(e)

    updater = asyncio.create_task(_periodic_updater())
    try:
        results: list[tuple[str, str | None]] = list(
            await asyncio.gather(*[deploy_one(ip) for ip in ips])
        )
    finally:
        updater.cancel()
        try:
            await updater
        except asyncio.CancelledError:
            pass

    successes = [ip for ip, err in results if err is None]
    failures = [(ip, err) for ip, err in results if err is not None]

    result_lines = []
    for ip, err in results:
        result_lines.append(f"✅ {ip}" if err is None else f"❌ {ip} — {str(err)[:80]}")
    result_text = "\n".join(result_lines)

    if not successes:
        await state.clear()
        await status_msg.edit_text(f"❌ Развёртывание завершилось с ошибкой:\n\n{result_text}")
        return

    if panel_id is None:
        await state.clear()
        suffix = f"\n\n⚠️ {len(failures)} нод(ы) не удалось установить." if failures else ""
        await status_msg.edit_text(f"✅ Результат:\n\n{result_text}{suffix}")
        return

    # Remnawave — load config profiles
    fail_suffix = f"\n⚠️ {len(failures)} нод(ы) не удалось установить." if failures else ""
    await status_msg.edit_text(
        f"✅ Установлено {len(successes)} из {n} нод.{fail_suffix}\n\nЗагружаю профили Remnawave..."
    )

    try:
        profiles = await get_config_profiles(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await state.clear()
        await status_msg.edit_text(
            f"✅ Результат:\n{result_text}\n\n"
            f"❌ Не удалось загрузить профили Remnawave:\n{e}\n\nДобавьте ноды вручную."
        )
        return

    if not profiles:
        await state.clear()
        await status_msg.edit_text(
            f"✅ Результат:\n{result_text}\n\n"
            "⚠️ Нет доступных config-профилей. Добавьте ноды вручную."
        )
        return

    await state.set_data({
        "panel_id": panel_id,
        "node_addresses": successes,
        "node_port": node_port,
    })
    await state.set_state(DeployRemnanode.choosing_config_profile)
    await status_msg.edit_text(
        f"✅ Результат:\n{result_text}\n\n"
        "Выберите Config Profile для нод в Remnawave:",
        reply_markup=config_profile_select_kb(profiles),
    )


@router.callback_query(
    DeployRemnanode.choosing_config_profile,
    F.data.regexp(r"^deploy:cfprofile:.+$"),
)
async def config_profile_chosen(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    profile_uuid = call.data.split(":", 2)[2]
    data = await state.get_data()
    panel_id: int = data["panel_id"]
    node_addresses: list[str] = data["node_addresses"]
    node_port: int = data["node_port"]

    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await state.clear()
        await call.message.edit_text("❌ Панель Remnawave не найдена.")
        return

    n = len(node_addresses)
    await call.message.edit_text(
        f"⏳ Добавляю {'ноду' if n == 1 else f'{n} ноды/нод'} в Remnawave..."
    )

    try:
        inbound_uuids = await get_profile_inbounds(panel.url, panel.api_token, profile_uuid)
    except RemnaWaveAPIError as e:
        await state.clear()
        await call.message.edit_text(f"❌ Ошибка получения инбаундов профиля:\n{e}")
        return

    async def create_one(address: str) -> tuple[str, dict | None, str | None]:
        try:
            node = await create_node(
                panel_url=panel.url,
                api_token=panel.api_token,
                name=address,
                address=address,
                port=node_port,
                profile_uuid=profile_uuid,
                inbound_uuids=inbound_uuids,
            )
            return address, node, None
        except RemnaWaveAPIError as e:
            return address, None, str(e)

    create_results = list(
        await asyncio.gather(*[create_one(addr) for addr in node_addresses])
    )

    await state.clear()

    lines = []
    for address, node, err in create_results:
        if err is None:
            lines.append(f"✅ {address}")
        else:
            lines.append(f"❌ {address} — {str(err)[:80]}")

    await call.message.edit_text(
        "Результат добавления нод в Remnawave:\n\n" + "\n".join(lines)
    )


# ─────────────────────────────────────────────
#  REMNANODE + HYSTERIA + TLS PRESET
# ─────────────────────────────────────────────

_HYSTERIA_PROTOCOLS = {"hysteria", "hysteria2"}
_HYS_PAGE_SIZE = 7


def _count_hysteria_inbounds(inbounds: list[dict]) -> int:
    return sum(
        1 for ib in inbounds
        if str(ib.get("type", "")).lower() in _HYSTERIA_PROTOCOLS
        or str(ib.get("network", "")).lower() in _HYSTERIA_PROTOCOLS
    )


def _inject_cert_paths(xray_config: dict, domain: str) -> tuple[dict, int]:
    config = copy.deepcopy(xray_config)
    cert_dir = f"/etc/letsencrypt/live/{domain}"
    count = 0
    for ib in config.get("inbounds", []):
        proto = str(ib.get("protocol", "")).lower()
        network = str(ib.get("streamSettings", {}).get("network", "")).lower()
        if proto in _HYSTERIA_PROTOCOLS or network in _HYSTERIA_PROTOCOLS:
            tls = ib.setdefault("streamSettings", {}).setdefault("tlsSettings", {})
            tls["certificates"] = [{
                "keyFile": f"{cert_dir}/privkey.pem",
                "certificateFile": f"{cert_dir}/fullchain.pem",
            }]
            count += 1
    return config, count


def _hys_domain_method_kb(has_cf: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_cf:
        rows.append([InlineKeyboardButton(
            text="📋 Список доменов Cloudflare",
            callback_data="deploy:hys:cfzones:0",
        )])
    rows.append([InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="deploy:hys:manual")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="deploy:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _hys_zones_kb(zones: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=z["name"], callback_data=f"deploy:hys:zone:{z['id']}")]
        for z in zones
    ]
    if total_pages > 1:
        prev = f"deploy:hys:cfzones:{page - 1}" if page > 0 else "deploy:hys:noop"
        nxt = f"deploy:hys:cfzones:{page + 1}" if page < total_pages - 1 else "deploy:hys:noop"
        rows.append([
            InlineKeyboardButton(text="◀️", callback_data=prev),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="deploy:hys:noop"),
            InlineKeyboardButton(text="▶️", callback_data=nxt),
        ])
    rows.append([InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="deploy:hys:manual")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="deploy:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Phase 1: Panel → IP → Login → Auth → Deploy → Profile ─────────────────────

@router.callback_query(F.data == "deploy:preset:remnanode_hysteria_tls")
async def preset_hysteria_tls(
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
            reply_markup=deploy_presets_kb(),
        )
        return
    await state.set_state(DeployHysteria.choosing_panel)
    await call.message.edit_text(
        "К какому Remnawave подключить ноду?",
        reply_markup=deploy_select_panel_kb(panels),
    )


@router.callback_query(DeployHysteria.choosing_panel, F.data == "deploy:panel:none")
async def hys_panel_none(call: CallbackQuery) -> None:
    await call.answer(
        "Для Hysteria+TLS пресета необходима панель Remnawave.",
        show_alert=True,
    )


@router.callback_query(
    DeployHysteria.choosing_panel,
    F.data.regexp(r"^deploy:panel:\d+$"),
)
async def hys_panel_chosen(
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
    await state.update_data(panel_id=panel_id)
    await state.set_state(DeployHysteria.waiting_ip)
    await call.message.edit_text(
        "Укажите IP адрес сервера:",
        reply_markup=deploy_cancel_kb(),
    )


@router.message(DeployHysteria.waiting_ip)
async def hys_got_ip(message: Message, state: FSMContext) -> None:
    ip = (message.text or "").strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        await message.answer("❌ Некорректный IP адрес. Попробуйте ещё раз:", reply_markup=deploy_cancel_kb())
        return
    await state.update_data(ip=ip)
    await state.set_state(DeployHysteria.waiting_login)
    await message.answer(f"IP: {ip}\n\nУкажите логин SSH-пользователя:", reply_markup=deploy_cancel_kb())


@router.message(DeployHysteria.waiting_login)
async def hys_got_login(message: Message, state: FSMContext) -> None:
    login = (message.text or "").strip()
    if not login:
        await message.answer("Логин не может быть пустым:", reply_markup=deploy_cancel_kb())
        return
    await state.update_data(login=login)
    await state.set_state(DeployHysteria.waiting_auth)
    await message.answer(
        "Отправьте пароль от сервера текстом, либо прикрепите файл с приватным SSH-ключом:",
        reply_markup=deploy_cancel_kb(),
    )


@router.message(DeployHysteria.waiting_auth, F.document)
async def hys_auth_got_key_file(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    file = await message.bot.get_file(message.document.file_id)
    file_io = await message.bot.download_file(file.file_path)
    key_bytes = file_io.read()
    try:
        parse_private_key(key_bytes)
    except SSHKeyPassphraseRequired:
        await state.update_data(auth_method="key", key_data_b64=base64.b64encode(key_bytes).decode())
        await state.set_state(DeployHysteria.waiting_key_passphrase)
        await message.answer("Этот ключ защищён key-phrase. Укажите key-phrase:", reply_markup=deploy_cancel_kb())
        return
    except SSHAuthError as e:
        await message.answer(f"{e}\nПришлите корректный файл с ключом:", reply_markup=deploy_cancel_kb())
        return
    await state.update_data(
        auth_method="key",
        key_data_b64=base64.b64encode(key_bytes).decode(),
        key_passphrase=None,
    )
    await _hys_deploy_node(message, state, active_org, session)


@router.message(DeployHysteria.waiting_auth)
async def hys_auth_got_password(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    password = (message.text or "").strip()
    if not password:
        await message.answer("Пришлите пароль текстом или файл с ключом:", reply_markup=deploy_cancel_kb())
        return
    await state.update_data(auth_method="password", password=password)
    await _hys_deploy_node(message, state, active_org, session)


@router.message(DeployHysteria.waiting_key_passphrase)
async def hys_auth_got_passphrase(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    passphrase = (message.text or "").strip()
    data = await state.get_data()
    key_bytes = base64.b64decode(data["key_data_b64"])
    try:
        parse_private_key(key_bytes, passphrase=passphrase)
    except SSHAuthError as e:
        await message.answer(f"{e}\nВведите key-phrase ещё раз:", reply_markup=deploy_cancel_kb())
        return
    await state.update_data(key_passphrase=passphrase)
    await _hys_deploy_node(message, state, active_org, session)


async def _hys_deploy_node(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    """Phase 1: deploy stable Remnanode, then show profile selector."""
    data = await state.get_data()
    ip: str = data["ip"]
    panel_id: int = data["panel_id"]

    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await state.clear()
        await message.answer("❌ Панель Remnawave не найдена.")
        return

    password = data.get("password")
    client_key = None
    if data.get("auth_method") == "key":
        key_bytes = base64.b64decode(data["key_data_b64"])
        client_key = parse_private_key(key_bytes, passphrase=data.get("key_passphrase"))

    status_msg = await message.answer(f"🚀 Разворачиваю Remnanode на {ip}...\n\n⏳ Подключаюсь...")

    async def progress(text: str) -> None:
        try:
            await status_msg.edit_text(f"🚀 Разворачиваю Remnanode на {ip}...\n\n{text}")
        except Exception:
            pass

    try:
        await deploy_remnanode(
            ip=ip,
            login=data["login"],
            password=password,
            client_key=client_key,
            secret_key=panel.node_secret_key,
            port=panel.node_port,
            progress_cb=progress,
        )
    except Exception as e:
        await state.clear()
        await status_msg.edit_text(f"❌ Ошибка развёртывания:\n{str(e)[:300]}")
        return

    await status_msg.edit_text(f"✅ Remnanode установлена на {ip}\n\n⏳ Загружаю профили...")

    try:
        profiles = await get_config_profiles(panel.url, panel.api_token)
    except RemnaWaveAPIError as e:
        await state.clear()
        await status_msg.edit_text(
            f"✅ Remnanode установлена на {ip}\n\n❌ Не удалось загрузить профили:\n{e}"
        )
        return

    if not profiles:
        await state.clear()
        await status_msg.edit_text(
            f"✅ Remnanode установлена на {ip}\n\n"
            "⚠️ Нет доступных config-профилей. Создайте профиль с Hysteria инбоундом в Remnawave."
        )
        return

    await state.set_state(DeployHysteria.choosing_config_profile)
    await status_msg.edit_text(
        f"✅ Remnanode установлена на {ip}\n\nВыберите Config Profile с Hysteria инбоундом:",
        reply_markup=config_profile_select_kb(profiles),
    )


@router.callback_query(
    DeployHysteria.choosing_config_profile,
    F.data.regexp(r"^deploy:cfprofile:.+$"),
)
async def hys_profile_chosen(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    profile_uuid = call.data.split(":", 2)[2]
    data = await state.get_data()
    panel_id: int = data["panel_id"]

    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await state.clear()
        await call.message.edit_text("❌ Панель Remnawave не найдена.")
        return

    await call.message.edit_text("🔍 Проверяю инбоунды профиля...")
    try:
        inbounds = await get_profile_inbounds_detailed(panel.url, panel.api_token, profile_uuid)
    except RemnaWaveAPIError as e:
        await call.message.edit_text(f"❌ Ошибка при загрузке инбоундов:\n{e}", reply_markup=deploy_cancel_kb())
        return

    hysteria_count = _count_hysteria_inbounds(inbounds)
    if hysteria_count == 0:
        try:
            profiles = await get_config_profiles(panel.url, panel.api_token)
        except RemnaWaveAPIError:
            profiles = []
        await call.message.edit_text(
            "⚠️ В выбранном профиле нет Hysteria инбоундов. Выберите другой:",
            reply_markup=config_profile_select_kb(profiles),
        )
        return

    await state.update_data(profile_uuid=profile_uuid, hysteria_count=hysteria_count)

    await state.set_state(DeployHysteria.choosing_zone_method)
    await call.message.edit_text(
        f"✅ {hysteria_count} Hysteria инбоунд(ов) найдено.\n\n"
        "Выберите домен для TLS-сертификата:",
        reply_markup=_hys_domain_method_kb(has_cf=True),
    )


# ── Phase 2: Domain selection ──────────────────────────────────────────────────

@router.callback_query(F.data == "deploy:hys:noop")
async def hys_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(
    DeployHysteria.choosing_zone_method,
    F.data.startswith("deploy:hys:cfzones:"),
)
async def hys_cf_zone_list(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    page = int(call.data.split(":")[-1])

    cf = await CloudflareService(session).get_or_create(active_org.id)
    if not cf.email or not cf.api_key:
        await call.message.edit_text(
            "❌ Cloudflare не настроен. Добавьте email и API Key в разделе Настройки → Cloudflare.",
            reply_markup=_hys_domain_method_kb(has_cf=False),
        )
        return

    try:
        zones = await list_zones(cf.email, cf.api_key)
    except CloudflareAPIError as e:
        await call.message.edit_text(
            f"❌ Ошибка Cloudflare API:\n{e}",
            reply_markup=_hys_domain_method_kb(has_cf=True),
        )
        return

    if not zones:
        await call.message.edit_text(
            "⚠️ В Cloudflare нет доступных зон.",
            reply_markup=_hys_domain_method_kb(has_cf=True),
        )
        return

    total_pages = (len(zones) + _HYS_PAGE_SIZE - 1) // _HYS_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    page_zones = zones[page * _HYS_PAGE_SIZE : (page + 1) * _HYS_PAGE_SIZE]

    await call.message.edit_text(
        "Выберите зону (домен 2-го уровня):",
        reply_markup=_hys_zones_kb(page_zones, page, total_pages),
    )


@router.callback_query(
    DeployHysteria.choosing_zone_method,
    F.data.startswith("deploy:hys:zone:"),
)
async def hys_zone_selected(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    zone_id = call.data[len("deploy:hys:zone:"):]

    cf = await CloudflareService(session).get_or_create(active_org.id)
    try:
        zone = await get_zone(cf.email, cf.api_key, zone_id)
    except CloudflareAPIError as e:
        await call.message.edit_text(f"❌ Ошибка CF:\n{e}", reply_markup=deploy_cancel_kb())
        return

    if not zone:
        await call.message.edit_text("❌ Зона не найдена.", reply_markup=deploy_cancel_kb())
        return

    zone_name = zone["name"]
    await state.update_data(zone_id=zone_id, zone_name=zone_name)
    await state.set_state(DeployHysteria.waiting_subdomain)
    await call.message.edit_text(
        f"Зона: <b>{zone_name}</b>\n\nВведите поддомен (например: <code>node1</code>):\n"
        f"→ будет создана запись <code>node1.{zone_name}</code>",
        reply_markup=deploy_cancel_kb(),
        parse_mode="HTML",
    )


@router.callback_query(
    DeployHysteria.choosing_zone_method,
    F.data == "deploy:hys:manual",
)
async def hys_manual_zone_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(DeployHysteria.waiting_manual_zone)
    await call.message.edit_text(
        "Введите зону (домен 2-го уровня), например: <code>example.com</code>",
        reply_markup=deploy_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(DeployHysteria.waiting_manual_zone)
async def hys_got_manual_zone(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    raw = (message.text or "").strip().lower().strip(".")
    if "." not in raw or len(raw) < 4:
        await message.answer(
            "❌ Введите корректный домен 2-го уровня, например: example.com",
            reply_markup=deploy_cancel_kb(),
        )
        return

    cf = await CloudflareService(session).get_or_create(active_org.id)
    if not cf.email or not cf.api_key:
        await message.answer(
            "❌ Cloudflare не настроен. Добавьте API-ключ в разделе Настройки.",
            reply_markup=deploy_cancel_kb(),
        )
        return

    try:
        result = await find_zone(cf.email, cf.api_key, raw)
    except CloudflareAPIError as e:
        await message.answer(f"❌ Ошибка Cloudflare API:\n{e}", reply_markup=deploy_cancel_kb())
        return

    if not result:
        await message.answer(
            f"❌ Зона <code>{raw}</code> не найдена в Cloudflare.\nПроверьте домен и попробуйте снова:",
            reply_markup=deploy_cancel_kb(),
            parse_mode="HTML",
        )
        return

    zone_id, zone_name = result
    await state.update_data(zone_id=zone_id, zone_name=zone_name)
    await state.set_state(DeployHysteria.waiting_subdomain)
    await message.answer(
        f"Зона: <b>{zone_name}</b>\n\nВведите поддомен (например: <code>node1</code>):\n"
        f"→ будет создана запись <code>node1.{zone_name}</code>",
        reply_markup=deploy_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(DeployHysteria.waiting_subdomain)
async def hys_got_subdomain(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    subdomain = (message.text or "").strip().lower().strip(".")
    if not subdomain or "." in subdomain:
        await message.answer(
            "❌ Введите только поддомен без точек, например: <code>node1</code>",
            reply_markup=deploy_cancel_kb(),
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    zone_id: str = data["zone_id"]
    zone_name: str = data["zone_name"]
    ip: str = data["ip"]
    panel_id: int = data["panel_id"]
    profile_uuid: str = data["profile_uuid"]
    full_domain = f"{subdomain}.{zone_name}"

    cf = await CloudflareService(session).get_or_create(active_org.id)
    svc = RemnaWaveService(session)
    panel = await svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await state.clear()
        await message.answer("❌ Панель Remnawave не найдена.")
        return

    status_msg = await message.answer(
        f"🚀 Настраиваю {full_domain}\n\n⏳ Создаю DNS запись..."
    )

    # ── 1. Create / update A record ───────────────────────────────────────────
    try:
        existing = await get_a_record(cf.email, cf.api_key, zone_id, full_domain)
        if existing:
            await update_a_record(cf.email, cf.api_key, zone_id, existing["id"], full_domain, ip)
        else:
            await create_a_record(cf.email, cf.api_key, zone_id, full_domain, ip)
    except CloudflareAPIError as e:
        await state.clear()
        await status_msg.edit_text(f"❌ Ошибка создания A-записи в Cloudflare:\n{e}")
        return

    # ── 2. Issue cert + restart remnanode with certs volume ───────────────────
    await status_msg.edit_text(
        f"🚀 {full_domain}\n\n✅ DNS запись создана\n⏳ Выпускаю TLS-сертификат..."
    )

    password = data.get("password")
    client_key = None
    if data.get("auth_method") == "key":
        key_bytes = base64.b64decode(data["key_data_b64"])
        client_key = parse_private_key(key_bytes, passphrase=data.get("key_passphrase"))

    async def progress(text: str) -> None:
        try:
            await status_msg.edit_text(
                f"🚀 {full_domain}\n\n✅ DNS создана\n{text}"
            )
        except Exception:
            pass

    try:
        await deploy_remnanode_hysteria(
            ip=ip,
            login=data["login"],
            password=password,
            client_key=client_key,
            domain=full_domain,
            letsencrypt_email=cf.email,
            node_secret=panel.node_secret_key,
            node_port=panel.node_port,
            progress_cb=progress,
        )
    except Exception as e:
        await state.clear()
        await status_msg.edit_text(
            f"✅ DNS запись создана: {full_domain} → {ip}\n\n"
            f"❌ Ошибка при выпуске сертификата:\n{str(e)[:300]}"
        )
        return

    # ── 3. Inject cert paths into profile ─────────────────────────────────────
    await status_msg.edit_text(
        f"🚀 {full_domain}\n\n✅ DNS создана\n✅ Сертификат выпущен\n⏳ Обновляю профиль..."
    )

    try:
        profile = await get_config_profile(panel.url, panel.api_token, profile_uuid)
        xray_config = profile.get("config") or {}
        updated_config, patched = _inject_cert_paths(xray_config, full_domain)
        await update_config_profile(panel.url, panel.api_token, profile_uuid, updated_config)
    except RemnaWaveAPIError as e:
        await state.clear()
        await status_msg.edit_text(
            f"✅ Сертификат выпущен для {full_domain}\n\n"
            f"❌ Не удалось обновить профиль:\n{e}\n\n"
            f"Пути: /etc/letsencrypt/live/{full_domain}/privkey.pem"
        )
        return

    # ── 4. Register node in Remnawave ─────────────────────────────────────────
    await status_msg.edit_text(
        f"🚀 {full_domain}\n\n✅ DNS создана\n✅ Сертификат выпущен\n"
        f"✅ Профиль обновлён ({patched} инбоунд(ов))\n⏳ Регистрирую ноду..."
    )

    try:
        inbound_uuids = await get_profile_inbounds(panel.url, panel.api_token, profile_uuid)
        await create_node(
            panel_url=panel.url,
            api_token=panel.api_token,
            name=full_domain,
            address=full_domain,
            port=panel.node_port,
            profile_uuid=profile_uuid,
            inbound_uuids=inbound_uuids,
        )
    except RemnaWaveAPIError as e:
        await state.clear()
        await status_msg.edit_text(
            f"✅ Сертификат выпущен\n✅ Профиль обновлён\n\n"
            f"❌ Не удалось зарегистрировать ноду в Remnawave:\n{e}"
        )
        return

    await state.clear()
    await status_msg.edit_text(
        f"✅ Готово!\n\n"
        f"🖥 Сервер: {ip}\n"
        f"🌐 Домен: {full_domain}\n"
        f"🔐 Сертификат: Let's Encrypt (авторенью 28-го числа)\n"
        f"📋 Профиль: обновлён ({patched} Hysteria инбоунд(ов))\n"
        f"🔗 Нода зарегистрирована в Remnawave"
    )
