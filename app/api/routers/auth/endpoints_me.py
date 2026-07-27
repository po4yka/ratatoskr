"""
Current-user endpoints (profile + account management).
"""

from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException

from app.api.dependencies.database import get_user_repository
from app.api.exceptions import ProcessingError
from app.api.models.responses import (
    SuccessFlagResponse,
    TypedSuccessResponse,
    UserInfo,
    success_response,
)
from app.api.routers.auth._fastapi import APIRouter, Depends
from app.api.routers.auth.dependencies import get_current_user
from app.api.services.auth_service import AuthService
from app.core.logging_utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _format_dt_z(dt_value: Any) -> str:
    if not dt_value:
        return ""
    if isinstance(dt_value, str):
        return dt_value if dt_value.endswith("Z") else dt_value + "Z"
    if hasattr(dt_value, "isoformat"):
        return str(dt_value.isoformat()) + "Z"
    s = str(dt_value)
    return s if s.endswith("Z") else s + "Z"


@router.get("/me", response_model=TypedSuccessResponse[UserInfo])
async def get_current_user_info(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Get current authenticated user information."""
    user_repo = get_user_repository()
    user_record, _ = await user_repo.async_get_or_create_user(
        user["user_id"],
        username=user.get("username"),
        is_owner=False,
    )

    return success_response(
        UserInfo(
            user_id=user["user_id"],
            username=user.get("username") or "",
            client_id=user["client_id"],
            is_owner=user_record.get("is_owner", False),
            created_at=_format_dt_z(user_record.get("created_at")),
        )
    )


_CONFIRM_DELETE_VALUE = "DELETE-MY-ACCOUNT"


@router.delete("/me", response_model=TypedSuccessResponse[SuccessFlagResponse])
async def delete_account(
    user: dict[str, Any] = Depends(get_current_user),
    x_confirm_delete: str | None = Header(None),
) -> Any:
    """Delete the current user account and all associated data.

    Requires the ``X-Confirm-Delete: DELETE-MY-ACCOUNT`` header as an
    explicit confirmation step to prevent accidental or CSRF-driven deletion.
    """
    if x_confirm_delete != _CONFIRM_DELETE_VALUE:
        raise HTTPException(
            status_code=400,
            detail=(
                "Account deletion requires the X-Confirm-Delete header "
                f"set to '{_CONFIRM_DELETE_VALUE}'."
            ),
        )

    user_id = user["user_id"]
    await AuthService.ensure_user(user_id)

    try:
        await AuthService.delete_user(user_id)
        logger.info("user_deleted_account", extra={"user_id": user_id})
        return success_response({"success": True})
    except Exception as e:
        logger.error("delete_account_failed", extra={"user_id": user_id}, exc_info=True)
        raise ProcessingError("Failed to delete account") from e
