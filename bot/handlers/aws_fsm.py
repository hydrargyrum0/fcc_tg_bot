from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline import (
    amazonws_kb,
    aws_detail_kb,
    aws_manage_kb,
    cancel_aws_edit_kb,
    cancel_aws_kb,
)
from bot.states.aws import AddAWS, EditAWS
from db.models.aws_account import AWSAccount
from db.models.organization import Organization
from services.aws_service import AWSService

router = Router()


def _account_detail_text(account: AWSAccount) -> str:
    return (
        f"AWS аккаунт — {account.tag}\n\n"
        f"Access Key ID: {account.access_key_id}\n"
        f"Secret Key: *****"
    )


async def _show_aws_list(
    message: Message,
    active_org: Organization,
    session: AsyncSession,
    edit: bool = False,
) -> None:
    svc = AWSService(session)
    accounts = await svc.get_org_accounts(active_org.id)

    if accounts:
        lines = "\n".join(f"• {a.tag} — {a.access_key_id}" for a in accounts)
        text = f"Подключенные AWS аккаунты:\n\n{lines}"
    else:
        text = "Подключенные AWS аккаунты:\n\nУ вас пока нет добавленных аккаунтов."

    if edit:
        await message.edit_text(text, reply_markup=amazonws_kb())
    else:
        await message.answer(text, reply_markup=amazonws_kb())


# ─────────────────────────────────────────────
#  ENTRY
# ─────────────────────────────────────────────

@router.callback_query(F.data == "settings:amazonws")
async def amazonws_cb(call: CallbackQuery, active_org: Organization, session: AsyncSession) -> None:
    await call.answer()
    await _show_aws_list(call.message, active_org, session, edit=True)


# ─────────────────────────────────────────────
#  ADD FLOW
# ─────────────────────────────────────────────

@router.callback_query(F.data == "aws:add")
async def aws_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AddAWS.waiting_access_key_id)
    await call.message.edit_text(
        "Укажите AWS Access Key ID:",
        reply_markup=cancel_aws_kb(),
    )


@router.message(AddAWS.waiting_access_key_id)
async def add_got_access_key_id(message: Message, state: FSMContext) -> None:
    await state.update_data(access_key_id=(message.text or "").strip())
    await state.set_state(AddAWS.waiting_secret_key)
    await message.answer(
        "Укажите AWS Secret Access Key:",
        reply_markup=cancel_aws_kb(),
    )


@router.message(AddAWS.waiting_secret_key)
async def add_got_secret_key(message: Message, state: FSMContext) -> None:
    await state.update_data(secret_access_key=(message.text or "").strip())
    await state.set_state(AddAWS.waiting_tag)
    await message.answer(
        "Укажите Тег для этого AWS аккаунта:",
        reply_markup=cancel_aws_kb(),
    )


@router.message(AddAWS.waiting_tag)
async def add_got_tag(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    svc = AWSService(session)
    await svc.add_account(
        org_id=active_org.id,
        tag=(message.text or "").strip(),
        access_key_id=data["access_key_id"],
        secret_access_key=data["secret_access_key"],
    )
    await state.clear()
    await _show_aws_list(message, active_org, session, edit=False)


@router.callback_query(F.data == "aws:add_cancel")
async def add_cancel(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    await state.clear()
    await _show_aws_list(call.message, active_org, session, edit=True)


# ─────────────────────────────────────────────
#  MANAGE FLOW
# ─────────────────────────────────────────────

@router.callback_query(F.data == "aws:manage")
async def manage_list(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = AWSService(session)
    accounts = await svc.get_org_accounts(active_org.id)

    if not accounts:
        await call.message.edit_text(
            "Подключенные AWS аккаунты:\n\nУ вас нет добавленных аккаунтов.",
            reply_markup=amazonws_kb(),
        )
        return

    lines = "\n".join(f"• {a.tag} — {a.access_key_id}" for a in accounts)
    await call.message.edit_text(
        f"Подключенные AWS аккаунты:\n\n{lines}",
        reply_markup=aws_manage_kb(accounts),
    )


@router.callback_query(F.data.regexp(r"^aws:\d+$"))
async def show_account_detail(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    account_id = int(call.data.split(":")[1])
    svc = AWSService(session)
    account = await svc.get_account_by_id(account_id, active_org.id)
    if not account:
        await call.answer("Аккаунт не найден.", show_alert=True)
        return
    await call.message.edit_text(
        _account_detail_text(account),
        reply_markup=aws_detail_kb(account.id),
    )


@router.callback_query(F.data == "aws:back")
async def back_to_manage_list(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    svc = AWSService(session)
    accounts = await svc.get_org_accounts(active_org.id)
    lines = "\n".join(f"• {a.tag} — {a.access_key_id}" for a in accounts)
    await call.message.edit_text(
        f"Подключенные AWS аккаунты:\n\n{lines}",
        reply_markup=aws_manage_kb(accounts),
    )


@router.callback_query(F.data.regexp(r"^aws:delete:\d+$"))
async def delete_account(
    call: CallbackQuery,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    account_id = int(call.data.split(":")[2])
    svc = AWSService(session)
    await svc.delete_account(account_id, active_org.id)
    await _show_aws_list(call.message, active_org, session, edit=True)


# ─────────────────────────────────────────────
#  EDIT FLOW
# ─────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^aws:etag:\d+$"))
async def edit_tag_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    account_id = int(call.data.split(":")[2])
    await state.set_state(EditAWS.waiting_tag)
    await state.update_data(account_id=account_id)
    await call.message.edit_text("Введите новый Тег:", reply_markup=cancel_aws_edit_kb())


@router.callback_query(F.data.regexp(r"^aws:ekey:\d+$"))
async def edit_access_key_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    account_id = int(call.data.split(":")[2])
    await state.set_state(EditAWS.waiting_access_key_id)
    await state.update_data(account_id=account_id)
    await call.message.edit_text("Введите новый Access Key ID:", reply_markup=cancel_aws_edit_kb())


@router.callback_query(F.data.regexp(r"^aws:esec:\d+$"))
async def edit_secret_key_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    account_id = int(call.data.split(":")[2])
    await state.set_state(EditAWS.waiting_secret_key)
    await state.update_data(account_id=account_id)
    await call.message.edit_text("Введите новый Secret Access Key:", reply_markup=cancel_aws_edit_kb())


@router.message(EditAWS.waiting_tag)
async def edit_got_tag(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    account_id: int = data["account_id"]
    svc = AWSService(session)
    await svc.update_tag(account_id, active_org.id, (message.text or "").strip())
    account = await svc.get_account_by_id(account_id, active_org.id)
    await state.clear()
    await message.answer(_account_detail_text(account), reply_markup=aws_detail_kb(account_id))


@router.message(EditAWS.waiting_access_key_id)
async def edit_got_access_key_id(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    account_id: int = data["account_id"]
    svc = AWSService(session)
    await svc.update_access_key_id(account_id, active_org.id, (message.text or "").strip())
    account = await svc.get_account_by_id(account_id, active_org.id)
    await state.clear()
    await message.answer(_account_detail_text(account), reply_markup=aws_detail_kb(account_id))


@router.message(EditAWS.waiting_secret_key)
async def edit_got_secret_key(
    message: Message,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    account_id: int = data["account_id"]
    svc = AWSService(session)
    await svc.update_secret_key(account_id, active_org.id, (message.text or "").strip())
    account = await svc.get_account_by_id(account_id, active_org.id)
    await state.clear()
    await message.answer(_account_detail_text(account), reply_markup=aws_detail_kb(account_id))


@router.callback_query(F.data == "aws:cancel_edit")
async def cancel_edit(
    call: CallbackQuery,
    state: FSMContext,
    active_org: Organization,
    session: AsyncSession,
) -> None:
    await call.answer()
    data = await state.get_data()
    account_id: int = data.get("account_id")
    await state.clear()

    if account_id:
        svc = AWSService(session)
        account = await svc.get_account_by_id(account_id, active_org.id)
        if account:
            await call.message.edit_text(
                _account_detail_text(account),
                reply_markup=aws_detail_kb(account_id),
            )
            return

    await _show_aws_list(call.message, active_org, session, edit=True)
