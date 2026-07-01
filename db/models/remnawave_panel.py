from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.models.base import Base


class RemnaWavePanel(Base):
    __tablename__ = "remnawave_panels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    tag: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_token: Mapped[str] = mapped_column(String(512), nullable=False)
    node_secret_key: Mapped[str] = mapped_column(Text, nullable=False)
    node_port: Mapped[int] = mapped_column(Integer, nullable=False)
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
