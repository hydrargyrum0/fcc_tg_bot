import asyncio
import base64
import ipaddress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    config_profile_select_kb,
    deploy_cancel_kb,
    deploy_presets_kb,
    deploy_select_panel_kb,
)
from bot.states.deploy import DeployRemnanode
from db.models.organization import Organization
from services.remnawave_api_service import RemnaWaveAPIError, create_node, get_config_profiles, get_profile_inbounds
from services.remnawave_service import RemnaWaveService
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
