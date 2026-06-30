from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.models.team import Team
from db.models.team_member import TeamMember
from db.models.user import User


class TeamService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_team(self, name: str, created_by: int) -> Team:
        team = Team(name=name, created_by=created_by)
        self.session.add(team)
        await self.session.commit()
        await self.session.refresh(team)
        return team

    async def get_team_by_id(self, team_id: int) -> Team | None:
        result = await self.session.execute(select(Team).where(Team.id == team_id))
        return result.scalar_one_or_none()

    async def list_teams(self) -> list[Team]:
        result = await self.session.execute(select(Team))
        return list(result.scalars().all())

    async def add_member(self, team_id: int, user_id: int) -> TeamMember:
        member = TeamMember(team_id=team_id, user_id=user_id)
        self.session.add(member)
        await self.session.commit()
        return member

    async def remove_member(self, team_id: int, user_id: int) -> bool:
        result = await self.session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if not member:
            return False
        await self.session.delete(member)
        await self.session.commit()
        return True

    async def get_user_team(self, user_id: int) -> Team | None:
        result = await self.session.execute(
            select(Team).join(TeamMember, Team.id == TeamMember.team_id).where(
                TeamMember.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def get_team_members(self, team_id: int) -> list[User]:
        result = await self.session.execute(
            select(User).join(TeamMember, User.id == TeamMember.user_id).where(
                TeamMember.team_id == team_id
            )
        )
        return list(result.scalars().all())
