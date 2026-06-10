"""Service logic for custom digest endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.api.dependencies.database import get_session_manager
from app.api.exceptions import ResourceNotFoundError, ValidationError
from app.api.models.responses import CustomDigestResponse
from app.api.search_helpers import isotime
from app.db.session import Database  # noqa: TC001  # used at runtime in __init__ signature
from app.infrastructure.persistence.repositories.user_content_repository import (
    UserContentRepositoryAdapter,
)

if TYPE_CHECKING:
    from app.api.models.requests import CreateCustomDigestRequest


class CustomDigestService:
    """Owns custom digest creation and retrieval."""

    def __init__(self, session_manager: Database | None = None) -> None:
        self._db = session_manager or get_session_manager()
        self._user_content_repo = UserContentRepositoryAdapter(self._db)

    async def create_digest(
        self,
        *,
        user_id: int,
        body: CreateCustomDigestRequest,
    ) -> dict[str, Any]:
        """Create a digest from owned summary IDs."""
        from app.application.services.topic_search_utils import ensure_mapping

        summary_id_ints: list[int] = []
        for sid in body.summary_ids:
            try:
                summary_id_ints.append(int(sid))
            except (ValueError, TypeError) as exc:
                raise ValidationError(
                    f"Invalid summary ID: {sid}", details={"summary_id": sid}
                ) from exc

        summaries = await self._user_content_repo.async_get_owned_summaries(
            user_id=user_id, summary_ids=summary_id_ints
        )
        found_ids = {
            int(summary.get("id")) for summary in summaries if summary.get("id") is not None
        }
        missing = [sid for sid in summary_id_ints if sid not in found_ids]
        if missing:
            raise ValidationError(
                "Some summary IDs not found or not owned by user",
                details={"missing_ids": [str(item) for item in missing]},
            )

        content_parts: list[str] = []
        for summary in summaries:
            request = summary.get("request") if isinstance(summary.get("request"), dict) else {}
            json_payload = ensure_mapping(summary.get("json_payload"))
            metadata = ensure_mapping(json_payload.get("metadata"))
            heading = (
                metadata.get("title") or request.get("input_url") or f"Summary {summary.get('id')}"
            )
            summary_text = json_payload.get("summary_250", "")
            content_parts.append(f"## {heading}\n\n{summary_text}")

        digest = await self._user_content_repo.async_create_custom_digest(
            user_id=user_id,
            title=body.title,
            summary_ids=summary_id_ints,
            format=body.format,
            content="\n\n---\n\n".join(content_parts),
        )
        return self._digest_to_response(digest).model_dump(by_alias=True)

    async def list_digests(self, *, user_id: int) -> list[dict[str, Any]]:
        """List digests owned by the user."""
        digests = await self._user_content_repo.async_list_custom_digests(user_id)
        return [self._digest_to_response(digest).model_dump(by_alias=True) for digest in digests]

    async def get_digest(self, *, user_id: int, digest_id: str) -> dict[str, Any]:
        """Get a single digest if owned by the user."""
        digest = await self._user_content_repo.async_get_custom_digest(digest_id)
        if digest is None:
            raise ResourceNotFoundError("CustomDigest", digest_id)
        if str(digest.get("user")) != str(user_id):
            raise ResourceNotFoundError("CustomDigest", digest_id)
        return self._digest_to_response(digest).model_dump(by_alias=True)

    @staticmethod
    def _digest_to_response(digest: Any) -> CustomDigestResponse:
        return CustomDigestResponse(
            id=str(digest.get("id")),
            title=str(digest.get("title") or ""),
            content=str(digest.get("content") or ""),
            status=str(digest.get("status") or ""),
            created_at=isotime(digest.get("created_at")),
        )
