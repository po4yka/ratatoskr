"""Models for outbound export integrations."""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - SQLAlchemy resolves string annotations at runtime.
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JSONB, JSONValue, _utcnow


class UserExportIntegration(Base):
    __tablename__ = "user_export_integrations"
    __table_args__ = (
        Index("ix_user_export_integrations_user_provider", "user_id", "provider"),
        Index("ix_user_export_integrations_user_enabled", "user_id", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    config_json: Mapped[JSONValue] = mapped_column(JSONB, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="export_integrations")
    deliveries: Mapped[list[Any]] = relationship(
        "ExportDeliveryLog", back_populates="integration", cascade="all, delete-orphan"
    )


class ExportDeliveryLog(Base):
    __tablename__ = "export_delivery_logs"
    __table_args__ = (
        Index("ix_export_delivery_logs_integration_id", "integration_id"),
        Index("ix_export_delivery_logs_summary_id", "summary_id"),
        Index("ix_export_delivery_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user_export_integrations.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    summary_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[JSONValue] = mapped_column(JSONB, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    integration: Mapped[UserExportIntegration] = relationship(back_populates="deliveries")


EXPORT_MODELS: tuple[type[Base], ...] = (UserExportIntegration, ExportDeliveryLog)


__all__ = ["EXPORT_MODELS", "ExportDeliveryLog", "UserExportIntegration"]
