from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class IpSet(Base):
    __tablename__ = "ip_sets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    tag: Mapped[str] = mapped_column(String(255), nullable=False)
    addresses: Mapped[str] = mapped_column(Text, nullable=False)  # newline-separated
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
