import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class UserRole(str, enum.Enum):
    superadmin = "superadmin"
    member = "member"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    active_org_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    notified_no_access: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)
