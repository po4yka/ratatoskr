"""Deterministic Qdrant point IDs shared by fast-path and CocoIndex writers."""

from __future__ import annotations

from app.core.uuid_utils import str_to_uuid

__all__ = ["repository_point_id", "str_to_uuid", "summary_point_id"]


def summary_point_id(request_id: int, summary_id: int) -> str:
    """Compute the Qdrant point UUID for a summary entity."""
    return str_to_uuid(f"{request_id}:{summary_id}")


def repository_point_id(environment: str, user_scope: str, repository_id: int) -> str:
    """Compute the Qdrant point UUID for a repository entity."""
    return str_to_uuid(f"{environment}:{user_scope}:repository:{repository_id}")
