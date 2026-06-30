from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from db.models.user import User, UserRole


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: int) -> User | None:
        result = await self.session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        user_id: int,
        username: str | None,
        full_name: str,
        role: UserRole,
    ) -> tuple[User, bool]:
        user = await self.get_by_id(user_id)
        if user:
            return user, False
        try:
            user = User(id=user_id, username=username, full_name=full_name, role=role)
            self.session.add(user)
            await self.session.commit()
            return user, True
        except IntegrityError:
            await self.session.rollback()
            user = await self.get_by_id(user_id)
            return user, False
