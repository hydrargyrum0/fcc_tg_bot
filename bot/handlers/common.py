from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from db.models.user import User, UserRole

router = Router()


@router.message(CommandStart())
async def start_handler(message: Message, db_user: User) -> None:
    if db_user.role == UserRole.superadmin:
        text = (
            f"Привет, суперадмин {db_user.full_name}!\n\n"
            "Доступные команды:\n"
            "/newteam <название> — создать команду\n"
            "/addmember <team_id> <user_id> — добавить участника\n"
            "/removemember <team_id> <user_id> — удалить участника\n"
            "/teams — список всех команд"
        )
    else:
        text = (
            f"Привет, {db_user.full_name}!\n\n"
            "Доступные команды:\n"
            "/myteam — посмотреть свою команду"
        )
    await message.answer(text)
