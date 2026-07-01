import base64
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import nd_cancel_kb, nd_overwrite_kb
from bot.states.node_domain import NodeDomainFSM
from db.models.organization import Organization
from db.models.user import User
from services.cloudflare_api import (
    CloudflareAPIError,
    create_a_record,
    delete_a_record,
    find_zone,
    get_a_record,
    update_a_record,
)
from services.cloudflare_service import CloudflareService
from services.remnawave_service import RemnaWaveService
from services.ssh_certbot_service import setup_domain_cert
from services.ssh_deploy_service import SSHAuthError, SSHKeyPassphraseRequired, parse_private_key

router = Router()

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]{1,251}[a-zA-Z0-9]$")


def _valid_ip(ip: str) -> bool:
    if not _IP_RE.match(ip):
        return False
    return all(0 <= int(o) <= 255 for o in ip.split("."))


def _valid_domain(domain: str) -> bool:
    return bool(_DOMAIN_RE.match(domain)) and domain.count(".") >= 2


def _back_to_node_kb(panel_id: int, node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к ноде", callback_data=f"nodes:node:{panel_id}:{node_uuid}")],
    ])


# ── Start ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^nodes:domain:\d+:.+$"))
async def start_domain_flow(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    parts = call.data.split(":", 3)
    panel_id = int(parts[2])
    node_uuid = parts[3]

    await state.set_data({"panel_id": panel_id, "node_uuid": node_uuid})
    await state.set_state(NodeDomainFSM.waiting_ip)
    await call.message.edit_text(
        "🌐 Привязка домена к ноде\n\nВведите IP-адрес сервера для A-записи:",
        reply_markup=nd_cancel_kb(panel_id, node_uuid),
    )


# ── Cancel (any state) ─────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^nd:cancel:\d+:.+$"))
async def cancel_domain_flow(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":", 3)
    panel_id = int(parts[2])
    node_uuid = parts[3]
    await state.clear()
    await call.answer()
    await call.message.edit_text(
        "❌ Операция отменена.",
        reply_markup=_back_to_node_kb(panel_id, node_uuid),
    )


# ── Step 1: IP ─────────────────────────────────────────────────────────────────

@router.message(NodeDomainFSM.waiting_ip)
async def got_ip(message: Message, state: FSMContext) -> None:
    ip = (message.text or "").strip()
    data = await state.get_data()
    panel_id, node_uuid = data["panel_id"], data["node_uuid"]

    if not _valid_ip(ip):
        await message.answer(
            "❌ Неверный формат IP-адреса. Введите IPv4, например: 1.2.3.4",
            reply_markup=nd_cancel_kb(panel_id, node_uuid),
        )
        return

    await state.update_data(ip=ip)
    await state.set_state(NodeDomainFSM.waiting_domain)
    await message.answer(
        f"IP: {ip}\n\nВведите домен (например, node.example.com):",
        reply_markup=nd_cancel_kb(panel_id, node_uuid),
    )


# ── Step 2: Domain → CF check ──────────────────────────────────────────────────

@router.message(NodeDomainFSM.waiting_domain)
async def got_domain(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    domain = (message.text or "").strip().lower()
    data = await state.get_data()
    panel_id, node_uuid, ip = data["panel_id"], data["node_uuid"], data["ip"]

    if not _valid_domain(domain):
        await message.answer(
            "❌ Неверный формат домена. Укажите полный домен, например: node.example.com",
            reply_markup=nd_cancel_kb(panel_id, node_uuid),
        )
        return

    cf_svc = CloudflareService(session)
    cf = await cf_svc.get_or_create(active_org.id)
    if not cf.email or not cf.api_key:
        await state.clear()
        await message.answer(
            "❌ Cloudflare не настроен.\n\n"
            "Перейдите в Настройки → Cloudflare и добавьте email и API Key.",
            reply_markup=_back_to_node_kb(panel_id, node_uuid),
        )
        return

    checking_msg = await message.answer("🔍 Проверяю зону в Cloudflare...")

    try:
        zone_result = await find_zone(cf.email, cf.api_key, domain)
    except CloudflareAPIError as e:
        await state.clear()
        await checking_msg.edit_text(
            f"❌ Ошибка Cloudflare API:\n{e}",
            reply_markup=_back_to_node_kb(panel_id, node_uuid),
        )
        return

    if zone_result is None:
        await state.clear()
        await checking_msg.edit_text(
            f"❌ Домен не найден в аккаунте Cloudflare.\n\n"
            f"Убедитесь, что зона для {domain} добавлена в ваш Cloudflare аккаунт.",
            reply_markup=_back_to_node_kb(panel_id, node_uuid),
        )
        return

    zone_id, root_domain = zone_result

    try:
        existing = await get_a_record(cf.email, cf.api_key, zone_id, domain)
    except CloudflareAPIError as e:
        await state.clear()
        await checking_msg.edit_text(
            f"❌ Ошибка при проверке DNS-записей:\n{e}",
            reply_markup=_back_to_node_kb(panel_id, node_uuid),
        )
        return

    await state.update_data(
        domain=domain,
        zone_id=zone_id,
        existing_record_id=existing["id"] if existing else None,
        existing_record_ip=existing["content"] if existing else None,
    )

    if existing:
        await state.set_state(NodeDomainFSM.waiting_overwrite_confirm)
        await checking_msg.edit_text(
            f"⚠️ Запись уже существует:\n"
            f"  {domain} → {existing['content']}\n\n"
            f"Перезаписать на {ip}?",
            reply_markup=nd_overwrite_kb(panel_id, node_uuid),
        )
    else:
        await state.set_state(NodeDomainFSM.waiting_ssh_login)
        await checking_msg.edit_text(
            f"✅ Зона {root_domain} найдена. Запись {domain} отсутствует.\n\n"
            f"Введите SSH логин сервера {ip}:",
            reply_markup=nd_cancel_kb(panel_id, node_uuid),
        )


# ── Overwrite confirm ──────────────────────────────────────────────────────────

@router.callback_query(NodeDomainFSM.waiting_overwrite_confirm, F.data == "nd:overwrite:yes")
async def overwrite_yes(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    panel_id, node_uuid, ip = data["panel_id"], data["node_uuid"], data["ip"]
    await state.set_state(NodeDomainFSM.waiting_ssh_login)
    await call.message.edit_text(
        f"Введите SSH логин сервера {ip}:",
        reply_markup=nd_cancel_kb(panel_id, node_uuid),
    )


# ── Step 3: SSH login ──────────────────────────────────────────────────────────

@router.message(NodeDomainFSM.waiting_ssh_login)
async def got_ssh_login(message: Message, state: FSMContext) -> None:
    login = (message.text or "").strip()
    data = await state.get_data()
    panel_id, node_uuid = data["panel_id"], data["node_uuid"]

    if not login:
        await message.answer(
            "❌ Логин не может быть пустым.",
            reply_markup=nd_cancel_kb(panel_id, node_uuid),
        )
        return

    await state.update_data(ssh_login=login)
    await state.set_state(NodeDomainFSM.waiting_auth)
    await message.answer(
        "Отправьте пароль от сервера текстом, либо прикрепите файл с приватным SSH-ключом:",
        reply_markup=nd_cancel_kb(panel_id, node_uuid),
    )


# ── Step 4a: Auth — SSH key file ───────────────────────────────────────────────

@router.message(NodeDomainFSM.waiting_auth, F.document)
async def auth_got_key_file(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    panel_id, node_uuid = data["panel_id"], data["node_uuid"]

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
        await state.set_state(NodeDomainFSM.waiting_key_passphrase)
        await message.answer(
            "Этот ключ защищён key-phrase. Укажите key-phrase от ключа:",
            reply_markup=nd_cancel_kb(panel_id, node_uuid),
        )
        return
    except SSHAuthError as e:
        await message.answer(
            f"{e}\nПришлите корректный файл с ключом:",
            reply_markup=nd_cancel_kb(panel_id, node_uuid),
        )
        return

    await state.update_data(
        auth_method="key",
        key_data_b64=base64.b64encode(key_bytes).decode(),
        key_passphrase=None,
    )
    await _run_domain_setup(message, state, active_org, session)


# ── Step 4b: Auth — password ───────────────────────────────────────────────────

@router.message(NodeDomainFSM.waiting_auth)
async def auth_got_password(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    password = (message.text or "").strip()
    data = await state.get_data()
    panel_id, node_uuid = data["panel_id"], data["node_uuid"]

    if not password:
        await message.answer(
            "Пришлите пароль текстом или файл с приватным ключом:",
            reply_markup=nd_cancel_kb(panel_id, node_uuid),
        )
        return

    await state.update_data(auth_method="password", password=password)
    await _run_domain_setup(message, state, active_org, session)


# ── Step 4c: Key passphrase ────────────────────────────────────────────────────

@router.message(NodeDomainFSM.waiting_key_passphrase)
async def auth_got_passphrase(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    passphrase = (message.text or "").strip()
    data = await state.get_data()
    panel_id, node_uuid = data["panel_id"], data["node_uuid"]
    key_bytes = base64.b64decode(data["key_data_b64"])

    try:
        parse_private_key(key_bytes, passphrase=passphrase)
    except SSHAuthError as e:
        await message.answer(
            f"{e}\nВведите key-phrase ещё раз:",
            reply_markup=nd_cancel_kb(panel_id, node_uuid),
        )
        return

    await state.update_data(key_passphrase=passphrase)
    await _run_domain_setup(message, state, active_org, session)


# ── Execute ────────────────────────────────────────────────────────────────────

async def _run_domain_setup(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    await state.clear()

    panel_id = data["panel_id"]
    node_uuid = data["node_uuid"]
    ip = data["ip"]
    domain = data["domain"]
    zone_id = data["zone_id"]
    existing_record_id: str | None = data.get("existing_record_id")
    existing_record_ip: str | None = data.get("existing_record_ip")
    ssh_login = data["ssh_login"]

    password: str | None = data.get("password")
    client_key = None
    if data.get("auth_method") == "key":
        key_bytes = base64.b64decode(data["key_data_b64"])
        client_key = parse_private_key(key_bytes, passphrase=data.get("key_passphrase"))

    cf_svc = CloudflareService(session)
    cf = await cf_svc.get_or_create(active_org.id)

    rw_svc = RemnaWaveService(session)
    panel = await rw_svc.get_panel_by_id(panel_id, active_org.id)
    if not panel:
        await message.answer(
            "❌ Панель не найдена.",
            reply_markup=_back_to_node_kb(panel_id, node_uuid),
        )
        return

    status_msg = await message.answer("⏳ Начинаю привязку домена...")

    async def progress(text: str) -> None:
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    cf_record_created = False
    cf_record_updated = False

    # Step 1: Cloudflare A record
    await progress(f"🌐 Создаю A-запись {domain} → {ip} в Cloudflare...")
    try:
        if existing_record_id:
            await update_a_record(cf.email, cf.api_key, zone_id, existing_record_id, domain, ip)
            cf_record_updated = True
        else:
            await create_a_record(cf.email, cf.api_key, zone_id, domain, ip)
            cf_record_created = True
    except CloudflareAPIError as e:
        await status_msg.edit_text(
            f"❌ Ошибка Cloudflare при создании A-записи:\n{e}",
            reply_markup=_back_to_node_kb(panel_id, node_uuid),
        )
        return

    # Step 2: SSH certbot
    try:
        await setup_domain_cert(
            ip=ip,
            login=ssh_login,
            password=password,
            client_key=client_key,
            domain=domain,
            letsencrypt_email=cf.email,
            node_secret=panel.node_secret_key,
            node_port=panel.node_port,
            progress_cb=progress,
        )
    except Exception as e:
        await progress(f"❌ Ошибка SSH: {e}\n\nОткатываю изменения...")
        try:
            if cf_record_created:
                record = await get_a_record(cf.email, cf.api_key, zone_id, domain)
                if record:
                    await delete_a_record(cf.email, cf.api_key, zone_id, record["id"])
            elif cf_record_updated and existing_record_id and existing_record_ip:
                await update_a_record(
                    cf.email, cf.api_key, zone_id, existing_record_id, domain, existing_record_ip
                )
        except Exception as rb_err:
            await status_msg.edit_text(
                f"❌ Ошибка SSH: {e}\n\n"
                f"⚠️ Откат CF также не удался: {rb_err}\n"
                f"Запись {domain} могла остаться изменённой.",
                reply_markup=_back_to_node_kb(panel_id, node_uuid),
            )
            return

        rb_note = (
            "A-запись в Cloudflare удалена (откат)."
            if cf_record_created
            else f"A-запись восстановлена → {existing_record_ip} (откат)."
            if cf_record_updated
            else ""
        )
        await status_msg.edit_text(
            f"❌ Ошибка при получении сертификата:\n{e}\n\n{rb_note}",
            reply_markup=_back_to_node_kb(panel_id, node_uuid),
        )
        return

    await status_msg.edit_text(
        f"✅ Домен {domain} успешно привязан к ноде!\n\n"
        f"• A-запись: {domain} → {ip}\n"
        f"• SSL-сертификат получен через Let's Encrypt\n"
        f"• Remnawave Node перезапущен с примонтированными сертификатами\n\n"
        f"Сертификаты на сервере:\n"
        f"/opt/certbot/certs/live/{domain}/",
        reply_markup=_back_to_node_kb(panel_id, node_uuid),
    )
