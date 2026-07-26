"""SQLAlchemy model for UI-managed service credentials.

Stores runtime provider secrets (LLM keys, scraper tokens, ...) encrypted at
rest with the same Fernet material as every other integration secret -- see
``app/security/secret_crypto.py``. Which keys are storable at all is decided by
``app/config/credential_catalog.py``, not by this table: key-encryption and
bootstrap secrets are excluded there and stay in ``.env``.

``credential_key`` is a plain string rather than a Postgres enum on purpose --
adding a provider should be a one-line catalog change, not a migration.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - SQLAlchemy resolves string annotations at runtime.

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import _utcnow


class ServiceCredential(Base):
    """One encrypted service credential owned by a user.

    The plaintext is never stored and never returned by the API. ``hint`` holds
    the last few characters so the UI can show which key is installed without a
    decrypt round-trip, and without ever shipping the secret to the browser.
    """

    __tablename__ = "service_credentials"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "credential_key", name="uq_service_credentials_user_credential"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    credential_key: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Display-only tail of the secret (e.g. "...a3f9"). Never the whole value.
    hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


CREDENTIAL_MODELS: tuple[type[Base], ...] = (ServiceCredential,)
