"""Outbound export integration management endpoints."""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.adapters.export.dispatcher import SUPPORTED_EXPORT_PROVIDERS, SummaryExportDispatcher
from app.api.dependencies.database import get_session_manager
from app.api.exceptions import APIException, ErrorCode, ResourceNotFoundError
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.api.search_helpers import isotime
from app.db.models import ExportDeliveryLog, UserExportIntegration
from app.db.types import _utcnow
from app.security.token_crypto import encrypt_token

router = APIRouter()
ExportProvider = Literal["notion", "readwise", "obsidian"]


class ExportIntegrationCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: ExportProvider
    name: str | None = Field(default=None, max_length=120)
    api_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        validation_alias="apiToken",
        serialization_alias="apiToken",
    )
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False


class ExportIntegrationUpdateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, max_length=120)
    api_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        validation_alias="apiToken",
        serialization_alias="apiToken",
    )
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class ExportIntegrationResponse(BaseModel):
    id: int
    provider: str
    name: str | None
    enabled: bool
    config: dict[str, Any]
    token_configured: bool = Field(serialization_alias="tokenConfigured")
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class ExportDeliveryLogResponse(BaseModel):
    id: int
    integration_id: int = Field(serialization_alias="integrationId")
    provider: str
    event_type: str = Field(serialization_alias="eventType")
    summary_id: int | None = Field(serialization_alias="summaryId")
    response_status: int | None = Field(serialization_alias="responseStatus")
    success: bool
    duration_ms: int | None = Field(serialization_alias="durationMs")
    error: str | None = None
    created_at: str = Field(serialization_alias="createdAt")


@router.get("/export-integrations")
async def list_export_integrations(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    db = get_session_manager()
    async with db.session() as session:
        rows = (
            await session.execute(
                select(UserExportIntegration)
                .where(UserExportIntegration.user_id == user["user_id"])
                .order_by(UserExportIntegration.created_at.desc())
            )
        ).scalars()
        return success_response(
            {"integrations": [_integration_response(row).model_dump(by_alias=True) for row in rows]}
        )


@router.post("/export-integrations", status_code=status.HTTP_201_CREATED)
async def create_export_integration(
    body: ExportIntegrationCreateRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _validate_provider_config(body.provider, body.config, token=body.api_token)
    db = get_session_manager()
    async with db.transaction() as session:
        row = UserExportIntegration(
            user_id=user["user_id"],
            provider=body.provider,
            name=body.name,
            encrypted_token=encrypt_token(body.api_token) if body.api_token else None,
            config_json=body.config,
            enabled=body.enabled,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return success_response(_integration_response(row))


@router.patch("/export-integrations/{integration_id}")
async def update_export_integration(
    body: ExportIntegrationUpdateRequest,
    integration_id: int = Path(..., ge=1),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    db = get_session_manager()
    async with db.transaction() as session:
        row = await _get_owned_integration(session, integration_id, user["user_id"])
        merged_config = body.config if body.config is not None else _config(row)
        token_present = body.api_token is not None or row.encrypted_token is not None
        _validate_provider_config(
            row.provider, merged_config, token="set" if token_present else None
        )
        if body.name is not None:
            row.name = body.name
        if body.api_token is not None:
            row.encrypted_token = encrypt_token(body.api_token)
        if body.config is not None:
            row.config_json = body.config
        if body.enabled is not None:
            row.enabled = body.enabled
        row.updated_at = _utcnow()
        await session.flush()
        await session.refresh(row)
        return success_response(_integration_response(row))


@router.delete("/export-integrations/{integration_id}")
async def revoke_export_integration(
    integration_id: int = Path(..., ge=1),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    db = get_session_manager()
    async with db.transaction() as session:
        row = await _get_owned_integration(session, integration_id, user["user_id"])
        row.enabled = False
        row.encrypted_token = None
        row.updated_at = _utcnow()
    return success_response({"revoked": True, "id": integration_id})


@router.get("/export-integrations/{integration_id}/deliveries")
async def list_export_deliveries(
    integration_id: int = Path(..., ge=1),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    db = get_session_manager()
    async with db.session() as session:
        await _get_owned_integration(session, integration_id, user["user_id"])
        rows = (
            await session.execute(
                select(ExportDeliveryLog)
                .where(ExportDeliveryLog.integration_id == integration_id)
                .order_by(ExportDeliveryLog.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return success_response(
            {"deliveries": [_delivery_response(row).model_dump(by_alias=True) for row in rows]}
        )


@router.post("/export-integrations/{integration_id}/test")
async def send_test_export_integration(
    integration_id: int = Path(..., ge=1),
    summary_id: int = Query(..., ge=1),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    db = get_session_manager()
    delivered = await SummaryExportDispatcher(db).publish_summary_created_to_integration(
        summary_id=summary_id,
        integration_id=integration_id,
        user_id=user["user_id"],
    )
    if not delivered:
        raise ResourceNotFoundError("Summary", summary_id)
    return success_response({"queued": True, "summaryId": summary_id})


async def _get_owned_integration(
    session: Any, integration_id: int, user_id: int
) -> UserExportIntegration:
    row = await session.get(UserExportIntegration, integration_id)
    if row is None or row.user_id != user_id:
        raise ResourceNotFoundError("ExportIntegration", integration_id)
    return cast("UserExportIntegration", row)


def _integration_response(row: UserExportIntegration) -> ExportIntegrationResponse:
    return ExportIntegrationResponse(
        id=row.id,
        provider=row.provider,
        name=row.name,
        enabled=row.enabled,
        config=_public_config(row.provider, _config(row)),
        token_configured=row.encrypted_token is not None,
        created_at=isotime(row.created_at) or "",
        updated_at=isotime(row.updated_at) or "",
    )


def _delivery_response(row: ExportDeliveryLog) -> ExportDeliveryLogResponse:
    return ExportDeliveryLogResponse(
        id=row.id,
        integration_id=row.integration_id,
        provider=row.provider,
        event_type=row.event_type,
        summary_id=row.summary_id,
        response_status=row.response_status,
        success=row.success,
        duration_ms=row.duration_ms,
        error=row.error,
        created_at=isotime(row.created_at) or "",
    )


def _config(row: UserExportIntegration) -> dict[str, Any]:
    return dict(row.config_json) if isinstance(row.config_json, dict) else {}


def _public_config(provider: str, config: dict[str, Any]) -> dict[str, Any]:
    if provider == "obsidian":
        return {
            key: value
            for key, value in config.items()
            if key in {"vault_path", "folder"} and isinstance(value, str)
        }
    if provider == "notion":
        return {
            "database_id": config.get("database_id")
            if isinstance(config.get("database_id"), str)
            else None
        }
    return {}


def _validate_provider_config(provider: str, config: dict[str, Any], *, token: str | None) -> None:
    if provider not in SUPPORTED_EXPORT_PROVIDERS:
        raise APIException(
            message=f"Unsupported export provider: {provider}",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )
    if provider in {"notion", "readwise"} and not token:
        raise APIException(
            message=f"{provider} export requires apiToken",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )
    if provider == "notion" and not isinstance(config.get("database_id"), str):
        raise APIException(
            message="Notion export requires config.database_id",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )
    if provider == "obsidian" and not isinstance(config.get("vault_path"), str):
        raise APIException(
            message="Obsidian export requires config.vault_path",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )
