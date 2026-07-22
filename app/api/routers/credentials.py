"""Owner-only management of UI-configurable service credentials.

Lets the deployment owner install provider keys (LLM, scraping, speech, ...)
from the web UI instead of editing ``.env`` and redeploying. Values take effect
without a restart -- see
``app/infrastructure/persistence/credential_store.py``.

Secrets are write-only across this boundary: no endpoint returns a stored
value, and responses carry presence flags plus a short display hint only. Which
keys are addressable at all is fixed by ``app/config/credential_catalog.py``;
key-encryption and bootstrap secrets are excluded there and a request naming
one is rejected as an unknown credential rather than acknowledged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.routers.auth import get_current_user
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.infrastructure.persistence.credential_store import CredentialStore

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/credentials", tags=["credentials"])


class CredentialItem(BaseModel):
    """Non-secret state of one catalog entry."""

    key: str = Field(description="Environment-variable name, also the storage key")
    label: str = Field(description="Human-readable provider name")
    group: str = Field(description="llm | embedding | speech | scraping | storage | ...")
    help_url: str | None = Field(default=None, description="Where to obtain this credential")
    configured_in_db: bool = Field(description="A value is installed through the UI")
    configured_in_env: bool = Field(description="A value is present in the deployed environment")
    hint: str | None = Field(
        default=None, description="Last four characters of the stored value, e.g. '...a3f9'"
    )


class CredentialListResponse(BaseModel):
    """Every UI-manageable credential and whether it is configured."""

    credentials: list[CredentialItem]


class CredentialSetRequest(BaseModel):
    """Body for ``PUT /{key}``."""

    value: str = Field(
        min_length=1,
        description="Plaintext secret. Encrypted at rest and never echoed back.",
    )


class CredentialSetResponse(BaseModel):
    """Result of installing a credential."""

    key: str
    hint: str | None = None


class CredentialDeleteResponse(BaseModel):
    """Result of clearing a credential."""

    key: str
    deleted: bool = Field(description="False when no UI-installed value existed")


def _get_app_config(request: Request) -> AppConfig:
    from app.di.api import resolve_api_runtime

    return resolve_api_runtime(request).cfg


def _get_store(request: Request) -> CredentialStore:
    from app.api.dependencies.database import get_session_manager
    from app.infrastructure.persistence.credential_store import CredentialStore

    return CredentialStore(get_session_manager(request))


def get_credentials_owner(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Restrict credential management to the configured deployment owner.

    These are deployment-wide service secrets, not per-user data: a second
    authenticated identity must never be able to read their presence or swap
    the key the whole bot summarizes with.
    """
    owner_id = next(iter(_get_app_config(request).telegram.allowed_user_ids), None)
    if owner_id is None or user["user_id"] != owner_id:
        raise HTTPException(status_code=403, detail="Credential management is owner-only")
    return user


@router.get("", response_model=CredentialListResponse)
async def list_credentials(
    request: Request,
    user: dict[str, Any] = Depends(get_credentials_owner),
) -> CredentialListResponse:
    """Return every catalog entry with presence flags -- never the values."""
    statuses = await _get_store(request).list_status(user_id=user["user_id"])
    return CredentialListResponse(
        credentials=[
            CredentialItem(
                key=s.key,
                label=s.label,
                group=s.group,
                help_url=s.help_url,
                configured_in_db=s.configured_in_db,
                configured_in_env=s.configured_in_env,
                hint=s.hint,
            )
            for s in statuses
        ]
    )


@router.put("/{key}", response_model=CredentialSetResponse)
async def set_credential(
    key: str,
    payload: CredentialSetRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_credentials_owner),
) -> CredentialSetResponse:
    """Install or replace a credential. Applies without a restart."""
    from app.infrastructure.persistence.credential_store import UnknownCredentialError

    try:
        hint = await _get_store(request).set_credential(
            user_id=user["user_id"], key=key, value=payload.value
        )
    except UnknownCredentialError:
        # Deliberately identical to any other unknown key: a forbidden secret
        # must not be distinguishable from a nonexistent one.
        raise HTTPException(status_code=404, detail=f"Unknown credential: {key}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CredentialSetResponse(key=key, hint=hint)


@router.delete("/{key}", response_model=CredentialDeleteResponse)
async def delete_credential(
    key: str,
    request: Request,
    user: dict[str, Any] = Depends(get_credentials_owner),
) -> CredentialDeleteResponse:
    """Remove a UI-installed credential, reverting to the environment value."""
    from app.infrastructure.persistence.credential_store import UnknownCredentialError

    try:
        deleted = await _get_store(request).delete_credential(user_id=user["user_id"], key=key)
    except UnknownCredentialError:
        raise HTTPException(status_code=404, detail=f"Unknown credential: {key}") from None
    return CredentialDeleteResponse(key=key, deleted=deleted)
