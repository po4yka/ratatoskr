"""Models for saved searches and opt-in search history."""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - SQLAlchemy resolves string annotations at runtime.
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JSONB, JSONValue, _utcnow


class SavedSearch(Base):
    __tablename__ = "saved_searches"
    __table_args__ = (
        Index("ix_saved_searches_user_created", "user_id", "created_at"),
        Index("ix_saved_searches_user_name", "user_id", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    filters_json: Mapped[JSONValue] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="saved_searches")


class SearchHistoryEntry(Base):
    __tablename__ = "search_history"
    __table_args__ = (Index("ix_search_history_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    filters_json: Mapped[JSONValue] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="search_history_entries")


SEARCH_MODELS: tuple[type[Base], ...] = (SavedSearch, SearchHistoryEntry)


__all__ = ["SEARCH_MODELS", "SavedSearch", "SearchHistoryEntry"]
