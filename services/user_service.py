from __future__ import annotations
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
            if user.username != username or user.full_name != full_name:
                user.username = username
                user.full_name = full_name
                await self.session.commit()
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

    async def set_active_org(self, user_id: int, org_id: int | None) -> None:
        await self.session.execute(
            update(User).where(User.id == user_id).values(active_org_id=org_id)
        )
        await self.session.commit()

    async def mark_notified_no_access(self, user_id: int) -> None:
        await self.session.execute(
            update(User).where(User.id == user_id).values(notified_no_access=True)
        )
        await self.session.commit()
