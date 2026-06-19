"""Collection and collaboration SQLAlchemy models."""

from __future__ import annotations

import datetime as dt  # noqa: TC003
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JSONB, JSONValue, _next_server_version, _utcnow


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (
        Index("ix_collections_user_id_name", "user_id", "name", unique=True),
        Index("ix_collections_user_id_parent_id_name", "user_id", "parent_id", "name"),
        Index("ix_collections_updated_at", "updated_at"),
        Index("ix_collections_parent_id_position", "parent_id", "position"),
        Index("ix_collections_collection_type", "collection_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("collections.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    share_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collection_type: Mapped[str] = mapped_column(Text, default="manual", nullable=False)
    query_conditions_json: Mapped[JSONValue] = mapped_column(JSONB, nullable=True)
    query_match_mode: Mapped[str] = mapped_column(Text, default="all", nullable=False)
    last_evaluated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[Any] = relationship("User", back_populates="collections")
    parent: Mapped[Any | None] = relationship(
        "Collection", remote_side=[id], back_populates="children"
    )
    children: Mapped[list[Any]] = relationship("Collection", back_populates="parent")
    items: Mapped[list[Any]] = relationship(
        "CollectionItem", back_populates="collection", cascade="all, delete-orphan"
    )
    collaborators: Mapped[list[Any]] = relationship(
        "CollectionCollaborator", back_populates="collection", cascade="all, delete-orphan"
    )
    invites: Mapped[list[Any]] = relationship(
        "CollectionInvite", back_populates="collection", cascade="all, delete-orphan"
    )


class CollectionItem(Base):
    __tablename__ = "collection_items"
    __table_args__ = (
        Index(
            "ix_collection_items_collection_id_summary_id",
            "collection_id",
            "summary_id",
            unique=True,
        ),
        Index("ix_collection_items_collection_id_position", "collection_id", "position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    summary_id: Mapped[int] = mapped_column(
        ForeignKey("summaries.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    collection: Mapped[Collection] = relationship(back_populates="items")
    summary: Mapped[Any] = relationship("Summary", back_populates="collection_items")


class CollectionCollaborator(Base):
    __tablename__ = "collection_collaborators"
    __table_args__ = (
        Index(
            "ix_collection_collaborators_collection_id_user_id",
            "collection_id",
            "user_id",
            unique=True,
        ),
        Index("ix_collection_collaborators_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="active", nullable=False)
    invited_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.telegram_user_id"), nullable=True
    )
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    collection: Mapped[Collection] = relationship(back_populates="collaborators")
    user: Mapped[Any] = relationship(
        "User", back_populates="collection_collaborations", foreign_keys=[user_id]
    )
    invited_by_user: Mapped[Any | None] = relationship(
        "User", back_populates="collection_invites_sent", foreign_keys=[invited_by_id]
    )


class CollectionInvite(Base):
    __tablename__ = "collection_invites"
    __table_args__ = (
        Index("ix_collection_invites_collection_id", "collection_id"),
        Index("ix_collection_invites_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    invited_user_id: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(Text, default="active", nullable=False)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    collection: Mapped[Collection] = relationship(back_populates="invites")


class CollectionPublicLink(Base):
    __tablename__ = "collection_public_links"
    __table_args__ = (
        Index("ix_collection_public_links_token", "token", unique=True),
        Index("ix_collection_public_links_collection_id", "collection_id"),
        Index("ix_collection_public_links_active", "collection_id", "revoked_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    collection: Mapped[Collection] = relationship()


COLLECTION_MODELS = (
    Collection,
    CollectionItem,
    CollectionCollaborator,
    CollectionInvite,
    CollectionPublicLink,
)
