from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.aws_account import AWSAccount


class AWSService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_org_accounts(self, org_id: int) -> list[AWSAccount]:
        result = await self.session.execute(
            select(AWSAccount)
            .where(AWSAccount.org_id == org_id)
            .order_by(AWSAccount.created_at)
        )
        return list(result.scalars().all())

    async def add_account(
        self,
        org_id: int,
        tag: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> AWSAccount:
        account = AWSAccount(
            org_id=org_id,
            tag=tag,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        return account

    async def get_account_by_id(self, account_id: int, org_id: int) -> AWSAccount | None:
        result = await self.session.execute(
            select(AWSAccount).where(
                AWSAccount.id == account_id,
                AWSAccount.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def update_tag(self, account_id: int, org_id: int, tag: str) -> bool:
        account = await self.get_account_by_id(account_id, org_id)
        if not account:
            return False
        account.tag = tag
        await self.session.commit()
        return True

    async def update_access_key_id(self, account_id: int, org_id: int, access_key_id: str) -> bool:
        account = await self.get_account_by_id(account_id, org_id)
        if not account:
            return False
        account.access_key_id = access_key_id
        await self.session.commit()
        return True

    async def update_secret_key(self, account_id: int, org_id: int, secret_access_key: str) -> bool:
        account = await self.get_account_by_id(account_id, org_id)
        if not account:
            return False
        account.secret_access_key = secret_access_key
        await self.session.commit()
        return True

    async def delete_account(self, account_id: int, org_id: int) -> bool:
        account = await self.get_account_by_id(account_id, org_id)
        if not account:
            return False
        await self.session.delete(account)
        await self.session.commit()
        return True
