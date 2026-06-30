import pytest
from services.user_service import UserService
from services.team_service import TeamService
from db.models.user import UserRole


@pytest.mark.asyncio
async def test_create_team(session):
    user_svc = UserService(session)
    admin, _ = await user_svc.get_or_create(1, "admin", "Admin", UserRole.superadmin)

    team_svc = TeamService(session)
    team = await team_svc.create_team(name="Alpha", created_by=admin.id)

    assert team.id is not None
    assert team.name == "Alpha"
    assert team.created_by == admin.id


@pytest.mark.asyncio
async def test_create_team_duplicate_name_raises(session):
    user_svc = UserService(session)
    admin, _ = await user_svc.get_or_create(1, "admin", "Admin", UserRole.superadmin)

    team_svc = TeamService(session)
    await team_svc.create_team("Alpha", admin.id)

    with pytest.raises(Exception):
        await team_svc.create_team("Alpha", admin.id)


@pytest.mark.asyncio
async def test_add_and_get_members(session):
    user_svc = UserService(session)
    admin, _ = await user_svc.get_or_create(1, "admin", "Admin", UserRole.superadmin)
    member, _ = await user_svc.get_or_create(2, "alice", "Alice", UserRole.member)

    team_svc = TeamService(session)
    team = await team_svc.create_team("Alpha", admin.id)
    await team_svc.add_member(team.id, member.id)

    members = await team_svc.get_team_members(team.id)
    assert len(members) == 1
    assert members[0].id == member.id


@pytest.mark.asyncio
async def test_remove_member(session):
    user_svc = UserService(session)
    admin, _ = await user_svc.get_or_create(1, "admin", "Admin", UserRole.superadmin)
    member, _ = await user_svc.get_or_create(2, "alice", "Alice", UserRole.member)

    team_svc = TeamService(session)
    team = await team_svc.create_team("Alpha", admin.id)
    await team_svc.add_member(team.id, member.id)
    removed = await team_svc.remove_member(team.id, member.id)

    assert removed is True
    members = await team_svc.get_team_members(team.id)
    assert len(members) == 0


@pytest.mark.asyncio
async def test_remove_nonexistent_member_returns_false(session):
    user_svc = UserService(session)
    admin, _ = await user_svc.get_or_create(1, "admin", "Admin", UserRole.superadmin)

    team_svc = TeamService(session)
    team = await team_svc.create_team("Alpha", admin.id)
    removed = await team_svc.remove_member(team.id, 999)

    assert removed is False


@pytest.mark.asyncio
async def test_get_user_team(session):
    user_svc = UserService(session)
    admin, _ = await user_svc.get_or_create(1, "admin", "Admin", UserRole.superadmin)
    member, _ = await user_svc.get_or_create(2, "alice", "Alice", UserRole.member)

    team_svc = TeamService(session)
    team = await team_svc.create_team("Alpha", admin.id)
    await team_svc.add_member(team.id, member.id)

    found = await team_svc.get_user_team(member.id)
    assert found is not None
    assert found.id == team.id


@pytest.mark.asyncio
async def test_get_user_team_returns_none_when_not_in_team(session):
    user_svc = UserService(session)
    member, _ = await user_svc.get_or_create(2, "alice", "Alice", UserRole.member)

    team_svc = TeamService(session)
    found = await team_svc.get_user_team(member.id)
    assert found is None


@pytest.mark.asyncio
async def test_list_teams(session):
    user_svc = UserService(session)
    admin, _ = await user_svc.get_or_create(1, "admin", "Admin", UserRole.superadmin)

    team_svc = TeamService(session)
    await team_svc.create_team("Alpha", admin.id)
    await team_svc.create_team("Beta", admin.id)

    teams = await team_svc.list_teams()
    assert len(teams) == 2
