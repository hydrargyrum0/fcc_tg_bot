import pytest
from unittest.mock import AsyncMock, MagicMock

from bot.middlewares.role import RoleMiddleware
from db.models.user import User, UserRole
from services.user_service import UserService


def make_tg_user(user_id: int, username: str, full_name: str):
    u = MagicMock()
    u.id = user_id
    u.username = username
    u.full_name = full_name
    return u


@pytest.mark.asyncio
async def test_role_middleware_creates_member(session):
    middleware = RoleMiddleware(superadmin_ids=[999])
    handler = AsyncMock()
    tg_user = make_tg_user(1, "alice", "Alice")

    data = {"session": session, "event_from_user": tg_user}
    await middleware(handler, MagicMock(), data)

    assert data["db_user"].role == UserRole.member
    handler.assert_called_once()


@pytest.mark.asyncio
async def test_role_middleware_creates_superadmin(session):
    middleware = RoleMiddleware(superadmin_ids=[42])
    handler = AsyncMock()
    tg_user = make_tg_user(42, "boss", "The Boss")

    data = {"session": session, "event_from_user": tg_user}
    await middleware(handler, MagicMock(), data)

    assert data["db_user"].role == UserRole.superadmin
    handler.assert_called_once()


@pytest.mark.asyncio
async def test_role_middleware_loads_existing_user(session):
    svc = UserService(session)
    await svc.get_or_create(5, "carol", "Carol", UserRole.member)

    middleware = RoleMiddleware(superadmin_ids=[])
    handler = AsyncMock()
    tg_user = make_tg_user(5, "carol", "Carol")

    data = {"session": session, "event_from_user": tg_user}
    await middleware(handler, MagicMock(), data)

    assert data["db_user"].id == 5
    handler.assert_called_once()


@pytest.mark.asyncio
async def test_role_middleware_skips_when_no_tg_user(session):
    middleware = RoleMiddleware(superadmin_ids=[])
    handler = AsyncMock()

    data = {"session": session}  # no event_from_user
    await middleware(handler, MagicMock(), data)

    assert "db_user" not in data
    handler.assert_called_once()
