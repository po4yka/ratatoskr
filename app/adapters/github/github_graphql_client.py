"""GitHub GraphQL client for star lists.

Star lists (the user-curated buckets shown on ``github.com/<user>?tab=stars``)
are absent from REST v3 — ``viewer.lists`` in GraphQL is the only source. This
client covers exactly that read; everything else about repositories keeps going
through :class:`~app.adapters.github.github_api_client.GitHubAPIClient`.

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
          name
          slug
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
            raise GitHubAuthError(f"GitHub GraphQL denied the star-list query: {messages}")
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
                    name=node.get("name") or "",
                    slug=node.get("slug") or "",
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
