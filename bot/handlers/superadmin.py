from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.filters.superadmin import SuperadminFilter
from db.models.user import User
from services.team_service import TeamService
from services.user_service import UserService

router = Router()
router.message.filter(SuperadminFilter())


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


@router.message(Command("addmember"))
async def add_member_handler(message: Message, session: AsyncSession) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Использование: /addmember <team_id> <user_id>")
        return

    team_id, user_id = int(parts[1]), int(parts[2])
    team_svc = TeamService(session)
    user_svc = UserService(session)

    team = await team_svc.get_team_by_id(team_id)
    if not team:
        await message.answer(f"Команда с ID {team_id} не найдена.")
        return

    target_user = await user_svc.get_by_id(user_id)
    if not target_user:
        await message.answer(f"Пользователь с ID {user_id} не найден. Пусть сначала напишет /start боту.")
        return

    try:
        await team_svc.add_member(team_id, user_id)
        await message.answer(f"Пользователь {target_user.full_name} добавлен в команду «{team.name}».")
    except Exception:
        await message.answer("Этот пользователь уже в команде.")


@router.message(Command("removemember"))
async def remove_member_handler(message: Message, session: AsyncSession) -> None:
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Использование: /removemember <team_id> <user_id>")
        return

    team_id, user_id = int(parts[1]), int(parts[2])
    svc = TeamService(session)

    team = await svc.get_team_by_id(team_id)
    if not team:
        await message.answer(f"Команда с ID {team_id} не найдена.")
        return

    removed = await svc.remove_member(team_id, user_id)
    if removed:
        await message.answer(f"Пользователь {user_id} удалён из команды «{team.name}».")
    else:
        await message.answer("Пользователь не найден в этой команде.")


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
