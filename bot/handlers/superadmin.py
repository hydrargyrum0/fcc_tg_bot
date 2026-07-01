from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.filters.superadmin import SuperadminFilter
from db.models.user import User
from services.organization_service import OrganizationService
from services.team_service import TeamService
from services.user_service import UserService

router = Router()
router.message.filter(SuperadminFilter())


@router.message(Command("neworg"))
async def new_org_handler(message: Message, db_user: User, session: AsyncSession) -> None:
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /neworg <название>")
        return

    name = args[1].strip()
    svc = OrganizationService(session)
    try:
        org = await svc.create_org(name=name, created_by=db_user.id)
        await message.answer(f"Организация «{org.name}» создана. ID: {org.id}")
    except Exception:
        await message.answer(f"Организация с названием «{name}» уже существует.")


@router.message(Command("addmember"))
async def add_member_handler(message: Message, session: AsyncSession) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Использование: /addmember <org_id> <user_id>")
        return

    org_id, user_id = int(parts[1]), int(parts[2])
    org_svc = OrganizationService(session)
    user_svc = UserService(session)

    org = await org_svc.get_org_by_id(org_id)
    if not org:
        await message.answer(f"Организация с ID {org_id} не найдена.")
        return

    target_user = await user_svc.get_by_id(user_id)
    if not target_user:
        await message.answer(f"Пользователь с ID {user_id} не найден. Пусть сначала напишет /start боту.")
        return

    member = await org_svc.add_member(org_id, user_id)
    if member:
        await message.answer(f"Пользователь {target_user.full_name} добавлен в «{org.name}».")
    else:
        await message.answer("Этот пользователь уже в организации.")


@router.message(Command("removemember"))
async def remove_member_handler(message: Message, session: AsyncSession) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Использование: /removemember <org_id> <user_id>")
        return

    org_id, user_id = int(parts[1]), int(parts[2])
    svc = OrganizationService(session)

    org = await svc.get_org_by_id(org_id)
    if not org:
        await message.answer(f"Организация с ID {org_id} не найдена.")
        return

    removed = await svc.remove_member(org_id, user_id)
    if removed:
        await message.answer(f"Пользователь {user_id} удалён из «{org.name}».")
    else:
        await message.answer("Пользователь не найден в этой организации.")


@router.message(Command("orgs"))
async def orgs_handler(message: Message, session: AsyncSession) -> None:
    svc = OrganizationService(session)
    orgs = await svc.get_all_orgs()

    if not orgs:
        await message.answer("Организаций пока нет. Создайте первую: /neworg <название>")
        return

    lines = [f"[{org.id}] {org.name}" for org in orgs]
    await message.answer("Организации:\n" + "\n".join(lines))


# ─── Legacy team commands (kept for backward compatibility) ───────────────────

@router.message(Command("newteam"))
async def new_team_handler(message: Message, db_user: User, session: AsyncSession) -> None:
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /newteam <название>")
        return

    name = args[1].strip()
    svc = TeamService(session)
    try:
        team = await svc.create_team(name=name, created_by=db_user.id)
        await message.answer(f"Команда «{team.name}» создана. ID: {team.id}")
    except Exception:
        await message.answer(f"Команда с названием «{name}» уже существует.")


@router.message(Command("teams"))
async def teams_handler(message: Message, session: AsyncSession) -> None:
    svc = TeamService(session)
    teams = await svc.list_teams()

    if not teams:
        await message.answer("Команд пока нет. Создайте первую: /newteam <название>")
        return

    lines = []
    for team in teams:
        members = await svc.get_team_members(team.id)
        lines.append(f"[{team.id}] {team.name} — {len(members)} участн.")

    await message.answer("Команды:\n" + "\n".join(lines))
