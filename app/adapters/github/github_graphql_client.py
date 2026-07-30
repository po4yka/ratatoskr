"""GitHub GraphQL client for stars and star lists.

Star lists (the user-curated buckets shown on ``github.com/<user>?tab=stars``)
are absent from REST v3 — ``viewer.lists`` in GraphQL is the only source. Star
mutations do exist in REST, but they are kept here so a single client owns
"change what the user has starred and how it is filed"; everything else about
repositories keeps going through
:class:`~app.adapters.github.github_api_client.GitHubAPIClient`.

Pagination is two-dimensional: lists are a connection, and each list's items are
their own connection. GraphQL cannot resume a nested connection directly, so a
list whose items overflow one page is re-selected with ``lists(first: 1, after:
<cursor of the preceding list>)`` and paged from there.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from app.adapters.github.exceptions import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubServerError,
)
from app.adapters.github.github_api_client import (
    _RATE_LIMIT_FALLBACK_SEC,
    _is_rate_limited,
    _rate_limit_reset_epoch,
)
from app.adapters.github.types import StarListDTO
from app.core.backoff import sleep_backoff
from app.core.logging_utils import get_logger, redact_headers_for_logging
from app.observability.metrics_repositories import GITHUB_API_RATE_LIMIT_HITS_TOTAL

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = get_logger(__name__)

_ITEMS_PAGE_SIZE = 100
_LISTS_PAGE_SIZE = 100

# First pass: every list plus its first page of items. ``edges.cursor`` is what
# makes the follow-up query below able to re-select a single list.
_LISTS_QUERY = """
query($listPageSize: Int!, $itemPageSize: Int!) {
  viewer {
    lists(first: $listPageSize) {
      edges {
        cursor
        node {
          id
          name
          slug
          description
          isPrivate
          items(first: $itemPageSize) {
            pageInfo { hasNextPage endCursor }
            nodes { ... on Repository { databaseId } }
          }
        }
      }
    }
  }
}
"""

# Follow-up pass: one list (the one right after $listCursor), next page of items.
_LIST_ITEMS_QUERY = """
query($listCursor: String, $itemCursor: String, $itemPageSize: Int!) {
  viewer {
    lists(first: 1, after: $listCursor) {
      nodes {
        name
        items(first: $itemPageSize, after: $itemCursor) {
          pageInfo { hasNextPage endCursor }
          nodes { ... on Repository { databaseId } }
        }
      }
    }
  }
}
"""


# Metadata only — no items. Resolving a list by slug for a mutation must not
# pay for paging every repository in every list.
_LIST_SUMMARIES_QUERY = """
query($listPageSize: Int!) {
  viewer {
    lists(first: $listPageSize) {
      nodes { id name slug description isPrivate }
    }
  }
}
"""


# --- Mutations -------------------------------------------------------------
# Undocumented but present in the public schema. Every one of them needs the
# `user` OAuth scope; a token that can *read* lists is not necessarily allowed
# to write them, which is why the callers validate the scope up front.

_LIST_FIELDS = "id name slug description isPrivate"

_CREATE_LIST_MUTATION = f"""
mutation($name: String!, $description: String, $isPrivate: Boolean) {{
  createUserList(input: {{name: $name, description: $description, isPrivate: $isPrivate}}) {{
    list {{ {_LIST_FIELDS} }}
  }}
}}
"""

_UPDATE_LIST_MUTATION = f"""
mutation($listId: ID!, $name: String, $description: String, $isPrivate: Boolean) {{
  updateUserList(
    input: {{listId: $listId, name: $name, description: $description, isPrivate: $isPrivate}}
  ) {{
    list {{ {_LIST_FIELDS} }}
  }}
}}
"""

_DELETE_LIST_MUTATION = """
mutation($listId: ID!) {
  deleteUserList(input: {listId: $listId}) { user { login } }
}
"""

# Resolves the repository node ID; `repositories.github_id` stores the REST
# databaseId, which the mutation below does not accept.
_REPO_NODE_ID_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { id }
}
"""

_SET_ITEM_LISTS_MUTATION = """
mutation($itemId: ID!, $listIds: [ID!]!) {
  updateUserListsForItem(input: {itemId: $itemId, listIds: $listIds}) {
    lists { id name slug }
  }
}
"""

# Starring is documented, and unlike the list mutations it is satisfied by the
# `repo` / `public_repo` scope the integration already holds. Both mutations are
# idempotent server-side: starring an already-starred repository succeeds.
_ADD_STAR_MUTATION = """
mutation($starrableId: ID!) {
  addStar(input: {starrableId: $starrableId}) {
    starrable { ... on Repository { databaseId viewerHasStarred } }
  }
}
"""

_REMOVE_STAR_MUTATION = """
mutation($starrableId: ID!) {
  removeStar(input: {starrableId: $starrableId}) {
    starrable { ... on Repository { databaseId viewerHasStarred } }
  }
}
"""


class GitHubGraphQLClient:
    """Async GitHub GraphQL v4 client, scoped to star lists.

    Usage::

        async with GitHubGraphQLClient(token) as gh:
            lists = await gh.fetch_star_lists()
    """

    ENDPOINT = "https://api.github.com/graphql"

    def __init__(
        self,
        access_token: str,
        *,
        request_timeout_sec: float = 30.0,
        max_retries: int = 3,
        backoff_min_sec: float = 0.5,
        backoff_max_sec: float = 5.0,
        user_agent: str = "Ratatoskr/1.0",
    ) -> None:
        self._max_retries = max_retries
        self._backoff_min_sec = backoff_min_sec
        self._backoff_max_sec = backoff_max_sec
        self._client = httpx.AsyncClient(
            timeout=request_timeout_sec,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": user_agent,
            },
        )

    async def __aenter__(self) -> GitHubGraphQLClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST a GraphQL document and return ``data``.

        Raises:
            GitHubAuthError: on 401/403 or a permission error in ``errors``.
            GitHubRateLimitError: on a transport rate limit or a ``RATE_LIMITED``
                error type (GraphQL answers 200 with an errors array).
            GitHubServerError: on 5xx after retries, or a malformed payload.
        """
        last_exc: Exception | None = None

        for attempt in range(self._max_retries):
            t0 = time.monotonic()
            try:
                response = await self._client.post(
                    self.ENDPOINT,
                    json={"query": query, "variables": variables},
                )
                logger.debug(
                    "github_graphql_request",
                    extra={
                        "url": self.ENDPOINT,
                        "status": response.status_code,
                        "duration_ms": int((time.monotonic() - t0) * 1000),
                        "attempt": attempt + 1,
                        "request_headers": redact_headers_for_logging(
                            dict(response.request.headers)
                        ),
                    },
                )

                status = response.status_code
                if status == 401:
                    raise GitHubAuthError("GitHub GraphQL returned 401 Unauthorized")
                if _is_rate_limited(response):
                    if GITHUB_API_RATE_LIMIT_HITS_TOTAL is not None:
                        GITHUB_API_RATE_LIMIT_HITS_TOTAL.inc()
                    raise GitHubRateLimitError(reset_epoch=_rate_limit_reset_epoch(response))
                if status == 403:
                    raise GitHubAuthError("GitHub GraphQL returned 403 Forbidden")
                if 500 <= status < 600:
                    last_exc = GitHubServerError(
                        f"GitHub GraphQL returned {status} (attempt {attempt + 1})"
                    )
                    if attempt < self._max_retries - 1:
                        await sleep_backoff(attempt, self._backoff_min_sec, self._backoff_max_sec)
                    continue

                payload = response.json()
                self._raise_for_graphql_errors(payload.get("errors") or [])
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise GitHubServerError("GitHub GraphQL response carried no data object")
                return data

            except (GitHubAuthError, GitHubRateLimitError):
                raise
            except httpx.NetworkError as exc:
                last_exc = exc
                logger.warning(
                    "github_graphql_network_error",
                    extra={"attempt": attempt + 1, "error": str(exc)},
                )
                if attempt < self._max_retries - 1:
                    await sleep_backoff(attempt, self._backoff_min_sec, self._backoff_max_sec)
                continue

        if last_exc is not None:
            raise last_exc
        raise GitHubServerError(f"All {self._max_retries} GraphQL attempts failed")

    @staticmethod
    def _raise_for_graphql_errors(errors: Iterable[dict[str, Any]]) -> None:
        """Translate a GraphQL errors array into the REST client's exception types."""
        errors = list(errors)
        if not errors:
            return
        types = {str(e.get("type", "")).upper() for e in errors}
        messages = "; ".join(str(e.get("message", "")) for e in errors)
        if "RATE_LIMITED" in types:
            # A GraphQL RATE_LIMITED error carries no reset header, so fall back
            # to the same grace period the REST client uses for a missing reset.
            raise GitHubRateLimitError(reset_epoch=int(time.time()) + _RATE_LIMIT_FALLBACK_SEC)
        if types & {"FORBIDDEN", "INSUFFICIENT_SCOPES", "UNAUTHORIZED"}:
            raise GitHubAuthError(f"GitHub GraphQL denied the request: {messages}")
        raise GitHubServerError(f"GitHub GraphQL returned errors: {messages}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_star_lists(self) -> list[StarListDTO]:
        """Return every star list of the authenticated user with its repo IDs.

        ``repo_github_ids`` holds ``databaseId`` values, which are the same
        integers as the REST ``repo.id`` already stored in ``repositories``.
        Non-repository items (a list can hold a deleted or inaccessible entry)
        come back as nodes without ``databaseId`` and are skipped.
        """
        data = await self._execute(
            _LISTS_QUERY,
            {"listPageSize": _LISTS_PAGE_SIZE, "itemPageSize": _ITEMS_PAGE_SIZE},
        )
        edges = self._dig(data, "viewer", "lists", "edges") or []

        results: list[StarListDTO] = []
        previous_cursor: str | None = None
        for edge in edges:
            node = edge.get("node") or {}
            items = node.get("items") or {}
            ids = self._database_ids(items.get("nodes") or [])
            page_info = items.get("pageInfo") or {}

            if page_info.get("hasNextPage"):
                ids.extend(
                    await self._drain_list_items(
                        list_cursor=previous_cursor,
                        item_cursor=page_info.get("endCursor"),
                    )
                )

            results.append(
                StarListDTO(
                    id=node.get("id") or "",
                    name=node.get("name") or "",
                    slug=node.get("slug") or "",
                    description=node.get("description") or "",
                    is_private=bool(node.get("isPrivate")),
                    repo_github_ids=ids,
                )
            )
            previous_cursor = edge.get("cursor")

        return results

    async def _drain_list_items(
        self,
        *,
        list_cursor: str | None,
        item_cursor: str | None,
    ) -> list[int]:
        """Page the remaining items of the list positioned after *list_cursor*."""
        collected: list[int] = []
        while item_cursor is not None:
            data = await self._execute(
                _LIST_ITEMS_QUERY,
                {
                    "listCursor": list_cursor,
                    "itemCursor": item_cursor,
                    "itemPageSize": _ITEMS_PAGE_SIZE,
                },
            )
            nodes = self._dig(data, "viewer", "lists", "nodes") or []
            if not nodes:
                break
            items = nodes[0].get("items") or {}
            collected.extend(self._database_ids(items.get("nodes") or []))
            page_info = items.get("pageInfo") or {}
            item_cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
        return collected

    async def fetch_star_list_summaries(self) -> list[StarListDTO]:
        """Return the lists without their items — one request, no item paging.

        Use this whenever only list identity matters (resolving a slug to a
        node ID for a mutation, rendering a picker); :meth:`fetch_star_lists`
        is the one that costs a page per 100 items per list.
        """
        data = await self._execute(_LIST_SUMMARIES_QUERY, {"listPageSize": _LISTS_PAGE_SIZE})
        nodes = self._dig(data, "viewer", "lists", "nodes") or []
        return [self._list_dto(node) for node in nodes]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def create_star_list(
        self,
        name: str,
        *,
        description: str = "",
        is_private: bool = False,
    ) -> StarListDTO:
        """Create a star list and return it.

        GitHub caps a user at 32 lists. The 33rd create fails server-side; the
        error surfaces as :class:`GitHubServerError` with GitHub's own message
        rather than being pre-empted here, so the cap stays GitHub's to enforce.
        """
        data = await self._execute(
            _CREATE_LIST_MUTATION,
            {"name": name, "description": description, "isPrivate": is_private},
        )
        return self._list_dto(self._dig(data, "createUserList", "list"))

    async def update_star_list(
        self,
        list_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        is_private: bool | None = None,
    ) -> StarListDTO:
        """Rename or re-describe a list. Omitted fields are left untouched."""
        data = await self._execute(
            _UPDATE_LIST_MUTATION,
            {
                "listId": list_id,
                "name": name,
                "description": description,
                "isPrivate": is_private,
            },
        )
        return self._list_dto(self._dig(data, "updateUserList", "list"))

    async def delete_star_list(self, list_id: str) -> None:
        """Delete a list. The repositories in it keep their stars."""
        await self._execute(_DELETE_LIST_MUTATION, {"listId": list_id})

    async def set_repository_lists(
        self,
        *,
        owner: str,
        name: str,
        list_ids: list[str],
    ) -> list[str]:
        """Replace the set of lists a repository belongs to; return list names.

        This mirrors the underlying mutation, which is a full overwrite rather
        than an add/remove: passing an empty ``list_ids`` removes the repo from
        every list. Callers that mean "also add to X" must read the current
        membership first.
        """
        item_id = await self._repository_node_id(owner=owner, name=name)
        data = await self._execute(
            _SET_ITEM_LISTS_MUTATION,
            {"itemId": item_id, "listIds": list_ids},
        )
        lists = self._dig(data, "updateUserListsForItem", "lists") or []
        return [entry.get("name") or "" for entry in lists if isinstance(entry, dict)]

    async def add_star(self, *, owner: str, name: str) -> None:
        """Star a repository on behalf of the authenticated user.

        Idempotent: GitHub accepts the mutation for an already-starred
        repository, so a caller does not have to read the current state first.
        Unlike the list mutations this needs no extra scope beyond the ``repo``
        the integration is already connected with.
        """
        starrable_id = await self._repository_node_id(owner=owner, name=name)
        await self._execute(_ADD_STAR_MUTATION, {"starrableId": starrable_id})

    async def remove_star(self, *, owner: str, name: str) -> None:
        """Unstar a repository. Idempotent, like :meth:`add_star`."""
        starrable_id = await self._repository_node_id(owner=owner, name=name)
        await self._execute(_REMOVE_STAR_MUTATION, {"starrableId": starrable_id})

    async def _repository_node_id(self, *, owner: str, name: str) -> str:
        """Resolve a repository's GraphQL node ID.

        ``repositories.github_id`` stores the REST ``databaseId``, which none of
        the mutations accept.
        """
        data = await self._execute(_REPO_NODE_ID_QUERY, {"owner": owner, "name": name})
        node_id = self._dig(data, "repository", "id")
        if not node_id:
            raise GitHubNotFoundError(f"GitHub has no repository {owner}/{name}")
        return str(node_id)

    @staticmethod
    def _list_dto(node: Any) -> StarListDTO:
        if not isinstance(node, dict):
            raise GitHubServerError("GitHub GraphQL returned no list object")
        return StarListDTO(
            id=node.get("id") or "",
            name=node.get("name") or "",
            slug=node.get("slug") or "",
            description=node.get("description") or "",
            is_private=bool(node.get("isPrivate")),
        )

    @staticmethod
    def _database_ids(nodes: Iterable[dict[str, Any]]) -> list[int]:
        return [n["databaseId"] for n in nodes if isinstance(n, dict) and n.get("databaseId")]

    @staticmethod
    def _dig(payload: dict[str, Any], *path: str) -> Any:
        cursor: Any = payload
        for key in path:
            if not isinstance(cursor, dict):
                return None
            cursor = cursor.get(key)
        return cursor
