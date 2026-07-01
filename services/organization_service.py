from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.organization import Organization
from db.models.organization_member import OrganizationMember
from db.models.user import User


class OrganizationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_org(self, name: str, created_by: int) -> Organization:
        org = Organization(name=name, created_by=created_by)
        self.session.add(org)
        await self.session.commit()
        await self.session.refresh(org)
        return org

    async def get_org_by_id(self, org_id: int) -> Organization | None:
        result = await self.session.execute(
            select(Organization).where(Organization.id == org_id)
        )
        return result.scalar_one_or_none()

    async def get_all_orgs(self) -> list[Organization]:
        result = await self.session.execute(
            select(Organization).order_by(Organization.created_at)
        )
        return list(result.scalars().all())

    async def add_member(self, org_id: int, user_id: int) -> OrganizationMember | None:
        try:
            member = OrganizationMember(org_id=org_id, user_id=user_id)
            self.session.add(member)
            await self.session.commit()
            await self.session.refresh(member)
            return member
        except IntegrityError:
            await self.session.rollback()
            return None

    async def remove_member(self, org_id: int, user_id: int) -> bool:
        result = await self.session.execute(
            select(OrganizationMember).where(
                OrganizationMember.org_id == org_id,
                OrganizationMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if not member:
            return False
        await self.session.delete(member)
        await self.session.commit()
        return True

    async def get_user_orgs(self, user_id: int) -> list[Organization]:
        result = await self.session.execute(
            select(Organization)
            .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
            .where(OrganizationMember.user_id == user_id)
            .order_by(Organization.name)
        )
        return list(result.scalars().all())

    async def is_member(self, org_id: int, user_id: int) -> bool:
        result = await self.session.execute(
            select(OrganizationMember).where(
                OrganizationMember.org_id == org_id,
                OrganizationMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def get_active_member_user_ids(self, org_id: int) -> list[int]:
        """Returns user IDs of members who have this org as their active org."""
        result = await self.session.execute(
            select(User.id)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .where(
                OrganizationMember.org_id == org_id,
                User.active_org_id == org_id,
            )
        )
        return list(result.scalars().all())
