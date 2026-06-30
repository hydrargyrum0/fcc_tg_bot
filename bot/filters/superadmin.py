from aiogram.filters import BaseFilter
from aiogram.types import Message

from db.models.user import User, UserRole


class SuperadminFilter(BaseFilter):
    async def __call__(self, message: Message, db_user: User) -> bool:
        return db_user.role == UserRole.superadmin
