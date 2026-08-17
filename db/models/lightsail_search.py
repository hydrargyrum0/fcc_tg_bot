"""Models for Lightsail automatic IP search."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class LightsailRegionConfig(Base):
    """Search config for one AWS account + Lightsail region pair."""

    __tablename__ = "lightsail_region_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    aws_account_id: Mapped[int] = mapped_column(
        ForeignKey("aws_accounts.id", ondelete="CASCADE"), nullable=False
    )
    # AWS region code, e.g. "us-east-1"
    region: Mapped[str] = mapped_column(String(50), nullable=False)
    # Display name fetched from AWS at config creation, e.g. "N. Virginia"
    region_display_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")

    # Status: idle | searching | paused | monitoring
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")

    # Search parameters
    target_count: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    recheck_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # Pingachock node UUIDs to use; None = all nodes
    node_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)

    # Runtime stats (reset on each search start)
    total_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    search_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_recheck_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Current Lightsail instance name (None when no instance running)
    instance_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LightsailStaticIp(Base):
    """A static IP address allocated in Lightsail for a search config."""

    __tablename__ = "lightsail_static_ips"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("lightsail_region_configs.id", ondelete="CASCADE"), nullable=False
    )

    # Lightsail resource name (unique in region, e.g. "fcc-42-a3b1")
    static_ip_name: Mapped[str] = mapped_column(String(100), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)

    # None = currently attached/being tested; True = working; False = not working
    is_working: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # True if currently attached to the search instance
    is_attached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
