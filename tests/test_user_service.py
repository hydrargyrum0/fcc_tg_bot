import pytest
from services.user_service import UserService
from db.models.user import UserRole


@pytest.mark.asyncio
async def test_get_or_create_new_user(session):
    svc = UserService(session)
    user, created = await svc.get_or_create(
        user_id=111,
        username="alice",
        full_name="Alice Smith",
        role=UserRole.member,
    )
    assert created is True
    assert user.id == 111
    assert user.username == "alice"
    assert user.role == UserRole.member


@pytest.mark.asyncio
async def test_get_or_create_existing_user(session):
    svc = UserService(session)
    await svc.get_or_create(111, "alice", "Alice Smith", UserRole.member)
    user, created = await svc.get_or_create(111, "alice", "Alice Smith", UserRole.member)
    assert created is False
    assert user.id == 111


@pytest.mark.asyncio
async def test_get_by_id_returns_none_when_missing(session):
    svc = UserService(session)
    user = await svc.get_by_id(999)
    assert user is None


@pytest.mark.asyncio
async def test_get_by_id_returns_user(session):
    svc = UserService(session)
    await svc.get_or_create(222, "bob", "Bob Jones", UserRole.superadmin)
    user = await svc.get_by_id(222)
    assert user is not None
    assert user.role == UserRole.superadmin


@pytest.mark.asyncio
async def test_get_or_create_handles_integrity_error(session):
    from sqlalchemy import text
    await session.execute(
        text("INSERT INTO users (id, username, full_name, role) VALUES (:id, :username, :full_name, :role)"),
        {"id": 333, "username": "dave", "full_name": "Dave", "role": "member"},
    )
    await session.commit()

    svc = UserService(session)
    user, created = await svc.get_or_create(333, "dave", "Dave", UserRole.member)
    assert created is False
    assert user.id == 333
