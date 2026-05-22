"""Webhook, rule, import, and backup SQLAlchemy models."""

from __future__ import annotations

import datetime as dt  # noqa: TC003
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JSONB, JSONValue, _next_server_version, _utcnow


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (Index("ix_webhook_subscriptions_user_id_enabled", "user_id", "enabled"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    events_json: Mapped[JSONValue] = mapped_column(JSONB, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="active", nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_delivery_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="webhooks")
    deliveries: Mapped[list[Any]] = relationship(
        "WebhookDelivery", back_populates="subscription", cascade="all, delete-orphan"
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_subscription_id", "subscription_id"),
        Index("ix_webhook_deliveries_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
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

    subscription: Mapped[WebhookSubscription] = relationship(back_populates="deliveries")


class AutomationRule(Base):
    __tablename__ = "automation_rules"
    __table_args__ = (
        Index("ix_automation_rules_user_id_enabled", "user_id", "enabled"),
        Index("ix_automation_rules_event_type", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    match_mode: Mapped[str] = mapped_column(Text, default="all", nullable=False)
    conditions_json: Mapped[JSONValue] = mapped_column(JSONB, default=list, nullable=False)
    actions_json: Mapped[JSONValue] = mapped_column(JSONB, default=list, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_triggered_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="rules")
    logs: Mapped[list[Any]] = relationship(
        "RuleExecutionLog", back_populates="rule", cascade="all, delete-orphan"
    )


class RuleExecutionLog(Base):
    __tablename__ = "rule_execution_logs"
    __table_args__ = (
        Index("ix_rule_execution_logs_rule_id", "rule_id"),
        Index("ix_rule_execution_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("automation_rules.id", ondelete="CASCADE"), nullable=False
    )
    summary_id: Mapped[int | None] = mapped_column(
        ForeignKey("summaries.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    conditions_result_json: Mapped[JSONValue] = mapped_column(JSONB, nullable=True)
    actions_taken_json: Mapped[JSONValue] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    rule: Mapped[AutomationRule] = relationship(back_populates="logs")
    summary: Mapped[Any | None] = relationship("Summary", back_populates="rule_execution_logs")


class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        Index("ix_import_jobs_user_id", "user_id"),
        Index("ix_import_jobs_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    source_format: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors_json: Mapped[JSONValue] = mapped_column(JSONB, default=list, nullable=False)
    options_json: Mapped[JSONValue] = mapped_column(JSONB, default=dict, nullable=False)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="import_jobs")


class UserBackup(Base):
    __tablename__ = "user_backups"
    __table_args__ = (
        Index("ix_user_backups_user_id", "user_id"),
        Index("ix_user_backups_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, default="manual", nullable=False)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_counts_json: Mapped[JSONValue] = mapped_column(JSONB, default=dict, nullable=False)
    schema_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="backups")


RULE_MODELS = (
    WebhookSubscription,
    WebhookDelivery,
    AutomationRule,
    RuleExecutionLog,
    ImportJob,
    UserBackup,
)
