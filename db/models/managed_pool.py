from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class ManagedPool(Base):
    __tablename__ = "managed_pools"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    host_tag: Mapped[str] = mapped_column(String(200), nullable=False)
    # JSON array of IpSet IDs that feed this pool
    ip_set_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    score_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=60.0)
    check_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ManagedIp(Base):
    __tablename__ = "managed_ips"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("managed_pools.id", ondelete="CASCADE"), nullable=False
    )
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    ping_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ping_loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tls_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tls_handshake_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    vless_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    vless_speed_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
