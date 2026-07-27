from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies.database import get_device_repository
from app.api.exceptions import ResourceNotFoundError
from app.api.models.responses import TypedSuccessResponse, success_response
from app.api.routers.auth import get_current_user
from app.core.logging_utils import get_logger

logger = get_logger(__name__)

router = APIRouter()


class DeviceRegistrationPayload(BaseModel):
    token: str = Field(..., min_length=1, max_length=500, description="FCM or APNS device token")
    platform: Literal["ios", "android"] = Field(..., description="Platform: 'ios' or 'android'")
    device_id: str | None = Field(None, description="Unique device identifier (optional)")

    model_config = ConfigDict(extra="ignore")


class DeviceRegistrationResponse(BaseModel):
    status: Literal["ok"]


@router.post(
    "/device",
    response_model=TypedSuccessResponse[DeviceRegistrationResponse],
)
async def register_device(
    payload: DeviceRegistrationPayload,
    user_data: Annotated[dict[str, Any], Depends(get_current_user)],
    device_repo: Any = Depends(get_device_repository),
) -> dict[str, Any]:
    """Register or update a device token for push notifications."""
    user_id = user_data["user_id"]

    try:
        await device_repo.async_upsert_device(
            user_id=user_id,
            token=payload.token,
            platform=payload.platform,
            device_id=payload.device_id,
        )
    except ValueError as exc:
        raise ResourceNotFoundError("User", user_id) from exc

    return success_response({"status": "ok"})
