"""Service logic for collections (nesting, sharing, move/reorder)."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
from typing import TYPE_CHECKING, Any, Literal, cast

from app.api.exceptions import (
    AuthorizationError,
    RateLimitExceededError,
    ResourceNotFoundError,
    ValidationError,
)
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.domain.services.smart_collection import (
    MAX_SMART_COLLECTIONS_PER_USER,
    evaluate_summary,
    validate_smart_conditions,
)
from app.domain.services.summary_context import build_summary_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

logger = get_logger(__name__)

Role = Literal["owner", "editor", "viewer"]
CollectionMembership = Literal["any", "owned", "shared"]
ROLE_RANK = {"owner": 3, "editor": 2, "viewer": 1}
BULK_COLLECTION_MAX_IDS = 500
PUBLIC_LINK_TOKEN_BYTES = 32
PUBLIC_LINK_PASSWORD_ITERATIONS = 120_000
PUBLIC_LINK_READ_MIN_INTERVAL_SECONDS = 1.0
_PUBLIC_LINK_READS: dict[tuple[str, str], dt.datetime] = {}


class CollectionService:
    """Business logic for collections and folders."""

    def __init__(self, repo_factory: Callable[[], Any]) -> None:
        self._repo_factory = repo_factory

    def _repo(self) -> Any:
        """Get a collection repository bound to the shared session manager."""
        return self._repo_factory()

    @staticmethod
    def _dedupe_ints(values: Iterable[int]) -> list[int]:
        """Return unique integer IDs in caller order."""
        seen: dict[int, None] = {}
        for value in values:
            seen.setdefault(int(value), None)
        return list(seen)

    @staticmethod
    def _require_batch_size(values: list[Any], operation: str) -> None:
        if len(values) > BULK_COLLECTION_MAX_IDS:
            raise ValidationError(f"{operation} accepts at most {BULK_COLLECTION_MAX_IDS} ids")

    # ---- access helpers ----
    @staticmethod
    async def _get_role(repo: Any, collection_id: int, user_id: int) -> Role | None:
        """Get user's role for a collection."""
        role = await repo.async_get_role(collection_id, user_id)
        if role in ("owner", "editor", "viewer"):
            return cast("Role", role)
        return None

    async def _require_role(
        self,
        repo: Any,
        collection_id: int,
        user_id: int,
        minimum: Role,
    ) -> Role:
        """Require at least a minimum role, raise AuthorizationError if insufficient."""
        role = await self._get_role(repo, collection_id, user_id)
        if role is None or ROLE_RANK[role] < ROLE_RANK[minimum]:
            raise AuthorizationError(f"Insufficient permissions for collection {collection_id}")
        return role

    @staticmethod
    async def _get_collection_or_raise(
        repo: Any,
        collection_id: int,
    ) -> dict[str, Any]:
        """Get collection or raise ResourceNotFoundError."""
        collection = await repo.async_get_collection(collection_id)
        if not collection:
            raise ResourceNotFoundError("Collection", collection_id)
        return cast("dict[str, Any]", collection)

    async def get_collection_with_auth(
        self,
        collection_id: int,
        user_id: int,
        minimum_role: Role,
    ) -> dict[str, Any]:
        """Get a collection with authorization check.

        Args:
            collection_id: The collection ID.
            user_id: The user ID requesting access.
            minimum_role: The minimum role required.

        Returns:
            Dict with collection data including item_count.

        Raises:
            ResourceNotFoundError: If collection not found.
            AuthorizationError: If user lacks required permissions.
        """
        repo = self._repo()
        collection = await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, minimum_role)
        return collection

    # ---- queries ----
    async def list_collections(
        self,
        user_id: int,
        parent_id: int | None,
        limit: int,
        offset: int,
        membership: CollectionMembership = "any",
    ) -> list[dict[str, Any]]:
        """List collections for a user with optional parent filter."""
        repo = self._repo()
        return cast(
            "list[dict[str, Any]]",
            await repo.async_list_collections(user_id, parent_id, limit, offset, membership),
        )

    async def get_tree(self, user_id: int, max_depth: int = 3) -> list[dict[str, Any]]:
        """Get collection tree for a user.

        Returns flat list of collections. Tree building done in memory.
        """
        repo = self._repo()
        collections = await repo.async_get_collection_tree(user_id)

        # Build tree in memory
        by_parent: dict[int | None, list[dict[str, Any]]] = {}
        for col in collections:
            parent_key = col.get("parent_id") or col.get("parent")
            by_parent.setdefault(parent_key, []).append(col)

        def build(node_parent: int | None, depth: int) -> list[dict[str, Any]]:
            if depth > max_depth:
                return []
            children = by_parent.get(node_parent, [])
            for child in children:
                child["_children"] = build(child.get("id"), depth + 1)
            return children

        return build(None, 1)

    # ---- helpers ----
    @staticmethod
    async def _guard_smart_collection(repo: Any, collection_id: int) -> None:
        """Raise ValidationError if the collection is a smart collection."""
        collection = await repo.async_get_collection(collection_id)
        if collection and collection.get("collection_type") == "smart":
            raise ValidationError("Cannot manually modify items in a smart collection")

    # ---- CRUD ----
    async def create_collection(
        self,
        *,
        user_id: int,
        name: str,
        description: str | None,
        parent_id: int | None,
        position: int | None,
        collection_type: str = "manual",
        query_conditions: list[dict[str, Any]] | None = None,
        query_match_mode: str = "all",
    ) -> dict[str, Any]:
        """Create a new collection."""
        repo = self._repo()

        # Validate parent if provided
        if parent_id is not None:
            parent = await repo.async_get_collection(parent_id)
            if not parent:
                raise ResourceNotFoundError("Collection", parent_id)
            await self._require_role(repo, parent_id, user_id, "editor")

        # Smart collection validation
        if collection_type == "smart":
            if not query_conditions:
                raise ValidationError("Smart collections must have at least one condition")
            valid, err = validate_smart_conditions(query_conditions, query_match_mode)
            if not valid:
                raise ValidationError(err or "Invalid smart collection conditions")
            # Check limit
            existing_smart = await repo.async_list_smart_collections_for_user(user_id)
            if len(existing_smart) >= MAX_SMART_COLLECTIONS_PER_USER:
                raise ValidationError(
                    f"Maximum of {MAX_SMART_COLLECTIONS_PER_USER} smart collections reached"
                )

        # Calculate position
        pos = position if position is not None else await repo.async_get_next_position(parent_id)

        # Shift existing positions
        await repo.async_shift_positions(parent_id, pos)

        # Create collection
        collection_id = await repo.async_create_collection(
            user_id=user_id,
            name=name,
            description=description,
            parent_id=parent_id,
            position=pos,
            collection_type=collection_type,
            query_conditions_json=query_conditions,
            query_match_mode=query_match_mode,
        )

        result = await repo.async_get_collection(collection_id)

        # Trigger initial evaluation for smart collections
        if collection_type == "smart" and result:
            try:
                await self.evaluate_smart_collection(collection_id, user_id)
                # Re-fetch to get updated item_count
                result = await repo.async_get_collection(collection_id)
            except Exception:
                logger.warning(
                    "smart_collection_initial_eval_failed",
                    extra={"collection_id": collection_id},
                    exc_info=True,
                )

        return result or {}

    async def update_collection(
        self,
        *,
        collection_id: int,
        user_id: int,
        name: str | None,
        description: str | None,
        parent_id: int | None = None,
        position: int | None = None,
        query_conditions: list[dict[str, Any]] | None = None,
        query_match_mode: str | None = None,
    ) -> dict[str, Any]:
        """Update a collection."""
        repo = self._repo()

        collection = await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "editor")

        updates: dict[str, Any] = {}
        current_parent_id = collection.get("parent_id") or collection.get("parent")
        is_smart = collection.get("collection_type") == "smart"
        conditions_changed = False

        # Handle parent change
        if parent_id is not None and parent_id != current_parent_id:
            if parent_id == collection_id:
                raise ValueError("Cannot set collection as its own parent")
            new_parent = await repo.async_get_collection(parent_id)
            if not new_parent:
                raise ResourceNotFoundError("Collection", parent_id)
            # Cycle check - need to walk up ancestors
            # For simplicity, use the move_collection method
            await self._require_role(repo, parent_id, user_id, "editor")
            updates["parent_id"] = parent_id

        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description

        # Handle smart collection condition updates
        if is_smart and query_conditions is not None:
            match_mode = query_match_mode or collection.get("query_match_mode", "all")
            valid, err = validate_smart_conditions(query_conditions, match_mode)
            if not valid:
                raise ValidationError(err or "Invalid smart collection conditions")
            updates["query_conditions_json"] = query_conditions
            conditions_changed = True
        if is_smart and query_match_mode is not None:
            updates["query_match_mode"] = query_match_mode
            conditions_changed = True

        # Handle position
        if position is not None:
            target_parent = updates.get("parent_id", current_parent_id)
            await repo.async_shift_positions(target_parent, position)
            updates["position"] = position
        elif "parent_id" in updates:
            # Moving to new parent, get next position
            new_pos = await repo.async_get_next_position(updates["parent_id"])
            updates["position"] = new_pos

        if updates:
            await repo.async_update_collection(collection_id, **updates)

        # Re-evaluate if conditions changed
        if is_smart and conditions_changed:
            try:
                await self.evaluate_smart_collection(collection_id, user_id)
            except Exception:
                logger.warning(
                    "smart_collection_re_eval_failed",
                    extra={"collection_id": collection_id},
                    exc_info=True,
                )

        result = await repo.async_get_collection(collection_id)
        return result or {}

    async def delete_collection(self, collection_id: int, user_id: int) -> None:
        """Soft delete a collection."""
        repo = self._repo()
        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "owner")
        await repo.async_soft_delete_collection(collection_id)

    # ---- items ----
    async def add_item(self, collection_id: int, summary_id: int, user_id: int) -> None:
        """Add a summary to a collection."""
        repo = self._repo()
        await self._guard_smart_collection(repo, collection_id)
        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "editor")
        if not await repo.async_summary_belongs_to_user(summary_id, user_id):
            raise ResourceNotFoundError("Summary", summary_id)

        position = await repo.async_get_next_item_position(collection_id)
        added = await repo.async_add_item(collection_id, summary_id, position)
        if not added:
            # Summary not found
            raise ResourceNotFoundError("Summary", summary_id)

    async def remove_item(self, collection_id: int, summary_id: int, user_id: int) -> None:
        """Remove a summary from a collection."""
        repo = self._repo()
        await self._guard_smart_collection(repo, collection_id)
        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "editor")
        await repo.async_remove_item(collection_id, summary_id)

    async def list_items(
        self, collection_id: int, user_id: int, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        """List items in a collection."""
        repo = self._repo()
        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "viewer")
        return cast(
            "list[dict[str, Any]]", await repo.async_list_items(collection_id, limit, offset)
        )

    async def reorder_items(
        self, collection_id: int, user_id: int, items: Iterable[dict[str, int]]
    ) -> None:
        """Reorder items in a collection."""
        repo = self._repo()
        item_list = list(items)
        self._require_batch_size(item_list, "reorder_items")
        await self._guard_smart_collection(repo, collection_id)
        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "editor")
        summary_ids = self._dedupe_ints(item["summary_id"] for item in item_list)
        if not summary_ids:
            return
        existing_ids = set(await repo.async_list_item_summary_ids(collection_id, summary_ids))
        if len(existing_ids) != len(summary_ids):
            missing = next(
                summary_id for summary_id in summary_ids if summary_id not in existing_ids
            )
            raise ResourceNotFoundError("Summary", missing)
        deduped_items: list[dict[str, int]] = []
        seen: set[int] = set()
        for item in item_list:
            summary_id = int(item["summary_id"])
            if summary_id in seen:
                continue
            seen.add(summary_id)
            deduped_items.append({"summary_id": summary_id, "position": int(item["position"])})
        await repo.async_reorder_items(collection_id, deduped_items)

    async def move_items(
        self,
        source_collection_id: int,
        user_id: int,
        summary_ids: list[int],
        target_collection_id: int,
        position: int | None,
    ) -> list[int]:
        """Move items from one collection to another."""
        repo = self._repo()
        summary_ids = self._dedupe_ints(summary_ids)
        self._require_batch_size(summary_ids, "move_items")

        # Guard against smart collections on both sides
        await self._guard_smart_collection(repo, source_collection_id)
        await self._guard_smart_collection(repo, target_collection_id)

        # Check both collections exist and user has editor access
        await self._get_collection_or_raise(repo, source_collection_id)
        await self._get_collection_or_raise(repo, target_collection_id)
        await self._require_role(repo, source_collection_id, user_id, "editor")
        await self._require_role(repo, target_collection_id, user_id, "editor")
        existing_source_ids = set(
            await repo.async_list_item_summary_ids(source_collection_id, summary_ids)
        )
        movable_summary_ids = [
            summary_id for summary_id in summary_ids if summary_id in existing_source_ids
        ]
        if not movable_summary_ids:
            return []

        return cast(
            "list[int]",
            await repo.async_move_items(
                source_collection_id, target_collection_id, movable_summary_ids, position
            ),
        )

    # ---- reorder / move collections ----
    async def reorder_collections(
        self, parent_id: int | None, user_id: int, items: Iterable[dict[str, int]]
    ) -> None:
        """Reorder collections within a parent."""
        repo = self._repo()
        item_list = list(items)
        self._require_batch_size(item_list, "reorder_collections")

        if parent_id is not None:
            await self._get_collection_or_raise(repo, parent_id)
            await self._require_role(repo, parent_id, user_id, "editor")

        collection_ids = self._dedupe_ints(item["collection_id"] for item in item_list)
        if not collection_ids:
            return
        for collection_id in collection_ids:
            collection = await self._get_collection_or_raise(repo, collection_id)
            if collection.get("parent_id") != parent_id and collection.get("parent") != parent_id:
                raise ResourceNotFoundError("Collection", collection_id)
            await self._require_role(repo, collection_id, user_id, "editor")
        deduped_items: list[dict[str, int]] = []
        seen: set[int] = set()
        for item in item_list:
            collection_id = int(item["collection_id"])
            if collection_id in seen:
                continue
            seen.add(collection_id)
            deduped_items.append(
                {"collection_id": collection_id, "position": int(item["position"])}
            )
        await repo.async_reorder_collections(parent_id, deduped_items)

    async def move_collection(
        self, collection_id: int, user_id: int, parent_id: int | None, position: int | None
    ) -> dict[str, Any]:
        """Move a collection to a new parent."""
        repo = self._repo()

        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "owner")

        if parent_id is not None:
            await self._get_collection_or_raise(repo, parent_id)
            await self._require_role(repo, parent_id, user_id, "editor")

        # Calculate position if not provided
        pos = position if position is not None else await repo.async_get_next_position(parent_id)

        result = await repo.async_move_collection(collection_id, parent_id, pos)
        if result is None:
            raise ValueError("Cycle detected or collection not found")
        return cast("dict[str, Any]", result)

    # ---- sharing ----
    async def list_acl(self, collection_id: int, user_id: int) -> list[dict[str, Any]]:
        """List access control entries for a collection."""
        repo = self._repo()

        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "viewer")

        # Get owner info
        owner_info = await repo.async_get_owner_info(collection_id)

        # Get collaborators
        collaborators = await repo.async_list_collaborators(collection_id)

        # Combine with owner as first entry
        result: list[dict[str, Any]] = []
        if owner_info:
            result.append(owner_info)
        result.extend(collaborators)
        return result

    async def add_collaborator(
        self, collection_id: int, user_id: int, target_user_id: int, role: Role
    ) -> None:
        """Add a collaborator to a collection."""
        repo = self._repo()

        collection = await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "owner")

        # Don't add owner as collaborator
        if target_user_id == collection.get("user_id"):
            return

        await repo.async_add_collaborator(collection_id, target_user_id, role, user_id)

    async def remove_collaborator(
        self, collection_id: int, user_id: int, target_user_id: int
    ) -> None:
        """Remove a collaborator from a collection."""
        repo = self._repo()

        collection = await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "owner")

        # Don't remove owner
        if target_user_id == collection.get("user_id"):
            return

        await repo.async_remove_collaborator(collection_id, target_user_id)

    async def create_invite(
        self,
        collection_id: int,
        user_id: int,
        role: Role,
        expires_at: datetime | None,
        recipient_user_id: int | None = None,
    ) -> dict[str, Any]:
        """Create an invite for a collection."""
        repo = self._repo()

        collection = await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "owner")
        if recipient_user_id == collection.get("user_id"):
            raise ValidationError("Cannot invite the collection owner")

        return cast(
            "dict[str, Any]",
            await repo.async_create_invite(
                collection_id,
                role,
                expires_at,
                invited_user_id=recipient_user_id,
            ),
        )

    async def list_incoming_invites(
        self,
        user_id: int,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """List pending collection invites addressed to a user."""
        repo = self._repo()
        return cast(
            "list[dict[str, Any]]",
            await repo.async_list_incoming_invites(user_id, limit, offset),
        )

    async def accept_invite(self, token: str, user_id: int) -> None:
        """Accept an invite to join a collection."""
        repo = self._repo()
        result = await repo.async_accept_invite(token, user_id)
        if result is None:
            raise ResourceNotFoundError("Invite", token)

    async def create_public_link(
        self,
        *,
        collection_id: int,
        user_id: int,
        expires_at: datetime | None,
        password: str | None,
    ) -> dict[str, Any]:
        """Create a read-only public link for a collection."""
        repo = self._repo()
        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "owner")
        token = secrets.token_urlsafe(PUBLIC_LINK_TOKEN_BYTES)
        return cast(
            "dict[str, Any]",
            await repo.async_create_public_link(
                collection_id=collection_id,
                token=token,
                expires_at=expires_at,
                password_hash=_hash_public_link_password(password) if password else None,
            ),
        )

    async def list_public_links(self, collection_id: int, user_id: int) -> list[dict[str, Any]]:
        """List public links for a collection."""
        repo = self._repo()
        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "owner")
        return cast("list[dict[str, Any]]", await repo.async_list_public_links(collection_id))

    async def revoke_public_link(self, collection_id: int, token: str, user_id: int) -> None:
        """Revoke a public collection link."""
        repo = self._repo()
        await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "owner")
        revoked = await repo.async_revoke_public_link(collection_id, token)
        if not revoked:
            raise ResourceNotFoundError("CollectionPublicLink", token)

    async def get_public_collection(
        self,
        *,
        token: str,
        password: str | None,
        viewer_ip: str | None,
    ) -> dict[str, Any]:
        """Load a public collection payload from a token."""
        repo = self._repo()
        link = await repo.async_get_public_link_by_token(token, include_password_hash=True)
        if not _active_public_link(link):
            raise ResourceNotFoundError("CollectionPublicLink", token)
        password_hash = link.get("password_hash") if link else None
        if isinstance(password_hash, str) and not _verify_public_link_password(
            password=password,
            encoded_hash=password_hash,
        ):
            raise ResourceNotFoundError("CollectionPublicLink", token)
        self._check_public_link_rate_limit(token=token, viewer_ip=viewer_ip)
        payload = await repo.async_get_public_collection_payload(token, viewer_ip=viewer_ip)
        if payload is None:
            raise ResourceNotFoundError("CollectionPublicLink", token)
        return cast("dict[str, Any]", payload)

    @staticmethod
    def _check_public_link_rate_limit(*, token: str, viewer_ip: str | None) -> None:
        if not viewer_ip:
            viewer_ip = "unknown"
        key = (token, viewer_ip)
        now = dt.datetime.now(UTC)
        previous = _PUBLIC_LINK_READS.get(key)
        if previous is not None:
            elapsed = (now - previous).total_seconds()
            if elapsed < PUBLIC_LINK_READ_MIN_INTERVAL_SECONDS:
                raise RateLimitExceededError(
                    retry_after_seconds=max(1, int(PUBLIC_LINK_READ_MIN_INTERVAL_SECONDS - elapsed))
                )
        _PUBLIC_LINK_READS[key] = now

    # ---- smart collections ----
    async def evaluate_smart_collection(self, collection_id: int, user_id: int) -> int:
        """Evaluate a smart collection against all user summaries.

        Loads the collection's conditions, evaluates each user summary,
        and replaces the collection's items with matching summaries.

        Args:
            collection_id: The smart collection ID.
            user_id: The owner/editor user ID.

        Returns:
            Count of matching items set in the collection.

        Raises:
            ResourceNotFoundError: If collection not found.
            ValidationError: If collection is not a smart collection.
        """
        repo = self._repo()
        collection = await self._get_collection_or_raise(repo, collection_id)
        await self._require_role(repo, collection_id, user_id, "editor")

        if collection.get("collection_type") != "smart":
            raise ValidationError("Collection is not a smart collection")

        conditions = collection.get("query_conditions_json")
        if isinstance(conditions, str):
            conditions = json.loads(conditions)
        if not conditions:
            raise ValidationError("Smart collection has no conditions")

        match_mode = collection.get("query_match_mode", "all")

        # Load all user summaries with request data
        summaries = await repo.async_list_user_summaries_with_request(user_id)

        # Evaluate each summary against conditions
        matching_ids: list[int] = []
        for entry in summaries:
            s_dict = entry.get("summary", {})
            r_dict = entry.get("request", {})
            context = build_summary_context(s_dict, r_dict)
            if evaluate_summary(conditions, context, match_mode):
                summary_id = s_dict.get("id")
                if summary_id is not None:
                    matching_ids.append(summary_id)

        # Replace collection items atomically
        count = int(await repo.async_bulk_set_items(collection_id, matching_ids))

        # Update last_evaluated_at
        await repo.async_update_collection(collection_id, last_evaluated_at=dt.datetime.now(UTC))

        logger.info(
            "smart_collection_evaluated",
            extra={
                "collection_id": collection_id,
                "user_id": user_id,
                "candidates": len(summaries),
                "matched": count,
            },
        )

        return count


def _hash_public_link_password(password: str) -> str:
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PUBLIC_LINK_PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PUBLIC_LINK_PASSWORD_ITERATIONS}${salt}${digest}"


def _verify_public_link_password(*, password: str | None, encoded_hash: str) -> bool:
    if not password:
        return False
    try:
        algorithm, iterations_raw, salt, expected = encoded_hash.split("$", 3)
        iterations = int(iterations_raw)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(actual, expected)


def _active_public_link(link: dict[str, Any] | None) -> bool:
    if not link or link.get("revoked_at") is not None:
        return False
    expires_at = link.get("expires_at")
    if expires_at is None:
        return True
    if isinstance(expires_at, str):
        try:
            expires_at = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if isinstance(expires_at, dt.datetime):
        normalized = (
            expires_at.astimezone(UTC) if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        )
        return normalized > dt.datetime.now(UTC)
    return False
