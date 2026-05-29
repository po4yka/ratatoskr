"""Channel digest SQLAlchemy models."""

from __future__ import annotations

import datetime as dt  # noqa: TC003
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JSONB, JSONValue, _utcnow


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (
        # Digest scheduling scans active channels ordered by staleness; a partial
        # index over active rows keyed on last_fetched_at serves that directly.
        Index(
            "ix_channels_active_last_fetched",
            "last_fetched_at",
            postgresql_where="is_active = true",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_fetched_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    fetch_error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    member_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    subscriptions: Mapped[list[Any]] = relationship(
        "ChannelSubscription", back_populates="channel", cascade="all, delete-orphan"
    )
    posts: Mapped[list[Any]] = relationship(
        "ChannelPost", back_populates="channel", cascade="all, delete-orphan"
    )
    signal_sources: Mapped[list[Any]] = relationship("Source", back_populates="legacy_channel")


class ChannelCategory(Base):
    __tablename__ = "channel_categories"
    __table_args__ = (Index("ix_channel_categories_user_id_name", "user_id", "name", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="channel_categories")
    subscriptions: Mapped[list[Any]] = relationship(
        "ChannelSubscription", back_populates="category"
    )
    rss_subscriptions: Mapped[list[Any]] = relationship(
        "RSSFeedSubscription", back_populates="category"
    )


class ChannelSubscription(Base):
    __tablename__ = "channel_subscriptions"
    __table_args__ = (
        Index("ix_channel_subscriptions_user_id_channel_id", "user_id", "channel_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel_categories.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="channel_subscriptions")
    channel: Mapped[Channel] = relationship(back_populates="subscriptions")
    category: Mapped[ChannelCategory | None] = relationship(back_populates="subscriptions")


class ChannelPost(Base):
    __tablename__ = "channel_posts"
    __table_args__ = (
        Index("ix_channel_posts_channel_id_message_id", "channel_id", "message_id", unique=True),
        Index("ix_channel_posts_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forwards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    channel: Mapped[Channel] = relationship(back_populates="posts")
    analysis: Mapped[Any | None] = relationship(
        "ChannelPostAnalysis", back_populates="post", cascade="all, delete-orphan"
    )
    signal_feed_items: Mapped[list[Any]] = relationship(
        "FeedItem", back_populates="legacy_channel_post"
    )


class ChannelPostAnalysis(Base):
    __tablename__ = "channel_post_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("channel_posts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    real_topic: Mapped[str] = mapped_column(Text, nullable=False)
    tldr: Mapped[str] = mapped_column(Text, nullable=False)
    key_insights: Mapped[JSONValue] = mapped_column(JSONB, nullable=True)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, default="other", nullable=False)
    llm_call_id: Mapped[int | None] = mapped_column(
        ForeignKey("llm_calls.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    post: Mapped[ChannelPost] = relationship(back_populates="analysis")
    llm_call: Mapped[Any | None] = relationship("LLMCall", back_populates="digest_analyses")


class DigestDelivery(Base):
    __tablename__ = "digest_deliveries"
    __table_args__ = (
        Index("ix_digest_deliveries_user_id", "user_id"),
        Index("ix_digest_deliveries_delivered_at", "delivered_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    delivered_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    channel_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    digest_type: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    posts_json: Mapped[JSONValue] = mapped_column(JSONB, nullable=True)

    user: Mapped[Any] = relationship("User", back_populates="digest_deliveries")


class UserDigestPreference(Base):
    __tablename__ = "user_digest_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    delivery_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(Text, nullable=True)
    hours_lookback: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_posts_per_digest: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="digest_preferences")


DIGEST_MODELS = (
    Channel,
    ChannelCategory,
    ChannelSubscription,
    ChannelPost,
    ChannelPostAnalysis,
    DigestDelivery,
    UserDigestPreference,
)
