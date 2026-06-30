from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.user import User
from services.team_service import TeamService

router = Router()


@router.message(Command("myteam"))
async def my_team_handler(message: Message, db_user: User, session: AsyncSession) -> None:
    svc = TeamService(session)
    team = await svc.get_user_team(db_user.id)

    if not team:
        await message.answer("Вы пока не состоите ни в одной команде.")
        return

    members = await svc.get_team_members(team.id)
    member_list = "\n".join(f"• {m.full_name} (@{m.username or 'нет'})" for m in members)
    await message.answer(
        f"Команда: {team.name} (ID: {team.id})\n\nУчастники:\n{member_list}"
    )
