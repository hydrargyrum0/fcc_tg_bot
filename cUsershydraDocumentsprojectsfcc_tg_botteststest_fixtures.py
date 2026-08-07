import pytest
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_engine_fixture(engine):
    """Test that engine fixture initializes properly"""
    assert engine is not None
    

@pytest.mark.asyncio
async def test_session_fixture(session):
    """Test that session fixture initializes properly"""
    assert isinstance(session, AsyncSession)
    

@pytest.mark.asyncio
async def test_session_isolation(session):
    """Test that expire_on_commit=False is working"""
    from db.models.user import User, UserRole
    
    user = User(
        id=123456,
        full_name="Test User",
        role=UserRole.member
    )
    session.add(user)
    await session.commit()
    
    assert user.id == 123456
    assert user.full_name == "Test User"
