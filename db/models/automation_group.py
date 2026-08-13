from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class AutomationGroup(Base):
    __tablename__ = "automation_groups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("remnawave_panels.id", ondelete="CASCADE"), nullable=False
    )
    host_tag: Mapped[str] = mapped_column(String(200), nullable=False)
    # JSON array of IpSet IDs.  Stored as-is; sets deleted externally are
    # silently skipped during pool build.
    ip_set_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    # 'same' — one IP applied to every host with the tag
    # 'each' — each bad host gets a separate replacement
    distribution: Mapped[str] = mapped_column(String(10), nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Optional: if set, replacements are sourced from a scored managed pool
    # rather than via real-time Pingachock scan of raw ip_set_ids.
    managed_pool_id: Mapped[int | None] = mapped_column(
        ForeignKey("managed_pools.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
