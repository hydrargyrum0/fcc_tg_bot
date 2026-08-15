from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class PingachockSettings(Base):
    __tablename__ = "pingachock_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), unique=True, nullable=False)
    api_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False)
    # List of Pingachock node UUIDs to use for checks.
    # NULL / empty list → {"all": True} (all non-virtual nodes).
    # Non-empty list   → {"node_ids": [...]} (only selected nodes).
    node_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
