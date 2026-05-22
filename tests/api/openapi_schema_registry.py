"""
Mapping from YAML spec schema names to Pydantic model classes.

This is the single source of truth for "which code model corresponds to which
OpenAPI spec schema."  The test_openapi_sync module uses this registry to verify
property names, required fields, and types stay in sync.
"""

from app.api.models.auth import (
    ClientSecretInfo,
    RefreshTokenRequest,
    SecretKeyActionResponse,
    SecretKeyCreateRequest,
    SecretKeyCreateResponse,
    SecretKeyListResponse,
    SecretKeyRevokeRequest,
    SecretKeyRotateRequest,
    SecretLoginRequest,
    SessionInfo,
    TelegramLinkBeginResponse,
    TelegramLinkCompleteRequest,
    TelegramLinkStatus,
    TelegramLoginRequest,
)
from app.api.models.requests import (
    CollectionCreateRequest,
    CollectionInviteRequest,
    CollectionItemCreateRequest,
    CollectionItemMoveRequest,
    CollectionItemReorderItem,
    CollectionItemReorderRequest,
    CollectionMoveRequest,
    CollectionReorderItem,
    CollectionReorderRequest,
    CollectionShareRequest,
    CollectionUpdateRequest,
    ForwardMetadata,
    SubmitForwardRequest,
    SubmitURLRequest,
    SyncApplyItem,
    SyncApplyRequest,
    SyncSessionRequest,
    UpdatePreferencesRequest,
    UpdateSummaryRequest,
)

# Maps YAML schema name -> Pydantic model class.
# Only covers schemas that have a direct 1:1 Pydantic counterpart in code.
# Envelope wrappers (e.g. LoginResponseEnvelope) are excluded because they are
# spec-only constructs built from these base schemas.
SCHEMA_REGISTRY: dict[str, type] = {
    # --- Authentication request models ---
    "TelegramLoginRequest": TelegramLoginRequest,
    "RefreshTokenRequest": RefreshTokenRequest,
    "SecretLoginRequest": SecretLoginRequest,
    "SecretKeyCreateRequest": SecretKeyCreateRequest,
    "SecretKeyRotateRequest": SecretKeyRotateRequest,
    "SecretKeyRevokeRequest": SecretKeyRevokeRequest,
    "TelegramLinkCompleteRequest": TelegramLinkCompleteRequest,
    # --- Authentication response data models ---
    "ClientSecretInfo": ClientSecretInfo,
    "SecretKeyCreateResponse": SecretKeyCreateResponse,
    "SecretKeyActionResponse": SecretKeyActionResponse,
    "SecretKeyListResponse": SecretKeyListResponse,
    "TelegramLinkStatus": TelegramLinkStatus,
    "TelegramLinkBeginResponse": TelegramLinkBeginResponse,
    "SessionInfo": SessionInfo,
    # --- Content request models ---
    "SubmitURLRequest": SubmitURLRequest,
    "SubmitForwardRequest": SubmitForwardRequest,
    "ForwardMetadata": ForwardMetadata,
    "UpdateSummaryRequest": UpdateSummaryRequest,
    "UpdatePreferencesRequest": UpdatePreferencesRequest,
    # --- Sync request models ---
    "SyncSessionRequest": SyncSessionRequest,
    "SyncApplyItem": SyncApplyItem,
    "SyncApplyRequest": SyncApplyRequest,
    # --- Collection request models ---
    "CollectionCreateRequest": CollectionCreateRequest,
    "CollectionUpdateRequest": CollectionUpdateRequest,
    "CollectionItemCreateRequest": CollectionItemCreateRequest,
    "CollectionReorderItem": CollectionReorderItem,
    "CollectionReorderRequest": CollectionReorderRequest,
    "CollectionItemReorderItem": CollectionItemReorderItem,
    "CollectionItemReorderRequest": CollectionItemReorderRequest,
    "CollectionMoveRequest": CollectionMoveRequest,
    "CollectionItemMoveRequest": CollectionItemMoveRequest,
    "CollectionShareRequest": CollectionShareRequest,
    "CollectionInviteRequest": CollectionInviteRequest,
}
