"""Deterministic Qdrant point IDs shared by the fast path and the reconciler."""

from __future__ import annotations

import uuid as _uuid

from app.core.uuid_utils import str_to_uuid

__all__ = [
    "git_mirror_point_id",
    "normalize_point_id",
    "repository_point_id",
    "str_to_uuid",
    "summary_point_id",
]


def normalize_point_id(raw_id: str) -> str:
    """Return the Qdrant point UUID for *raw_id*, hashing it only if it is a key.

    The vector store is handed two different kinds of identifier. Summaries and
    wiki pages pass a raw domain key -- ``"<request_id>:<summary_id>"``, a page
    path -- that still has to be hashed. Repositories and git mirrors pass a
    value already produced by the helpers below, which is a UUID.

    Hashing unconditionally treated the second kind as a key and stored the hash
    of a hash, so the id a caller computed in order to delete or address a point
    never matched the one Qdrant held. Search never noticed, because it matches
    on the vector and the payload rather than the id -- but every delete silently
    missed, leaving the point behind forever.
    """
    try:
        _uuid.UUID(raw_id)
    except (AttributeError, TypeError, ValueError):
        return str_to_uuid(raw_id)
    return raw_id


def summary_point_id(request_id: int, summary_id: int) -> str:
    """Compute the Qdrant point UUID for a summary entity."""
    return str_to_uuid(f"{request_id}:{summary_id}")


def repository_point_id(environment: str, user_scope: str, repository_id: int) -> str:
    """Compute the Qdrant point UUID for a repository entity."""
    return str_to_uuid(f"{environment}:{user_scope}:repository:{repository_id}")


def git_mirror_point_id(environment: str, user_scope: str, mirror_id: int) -> str:
    """Compute the Qdrant point UUID for a git mirror README entity."""
    return str_to_uuid(f"{environment}:{user_scope}:git_mirror:{mirror_id}")
