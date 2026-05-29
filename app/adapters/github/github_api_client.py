"""GitHub REST API client with retry, pagination, and auth-header redaction."""

from __future__ import annotations

import re
import time
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from app.adapters.github.exceptions import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubServerError,
)
from app.adapters.github.types import (
    AuthenticatedUserDTO,
    GistDTO,
    LanguagesDTO,
    RepositoryDTO,
    StarredItem,
)
from app.core.backoff import sleep_backoff
from app.core.logging_utils import get_logger

_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

_REDACTED_HEADER_KEYS = frozenset({"authorization", "token", "x-github-token"})

logger = get_logger(__name__)


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with sensitive values replaced by '***REDACTED***'."""
    return {
        k: ("***REDACTED***" if k.lower() in _REDACTED_HEADER_KEYS else v)
        for k, v in headers.items()
    }


class GitHubAPIClient:
    """Async GitHub REST API v3 client.

    Usage::

        async with GitHubAPIClient(token) as gh:
            repo = await gh.get_repo("tiangolo", "fastapi")
    """

    BASE_URL = "https://api.github.com"

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
        self._access_token = access_token
        self._max_retries = max_retries
        self._backoff_min_sec = backoff_min_sec
        self._backoff_max_sec = backoff_max_sec

        self._default_headers: dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": user_agent,
        }
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=request_timeout_sec,
            headers=self._default_headers,
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> GitHubAPIClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_next_link(self, link_header: str | None) -> str | None:
        """Extract the URL for rel="next" from a Link header, or None."""
        if not link_header:
            return None
        m = _LINK_NEXT_RE.search(link_header)
        return m.group(1) if m else None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute an HTTP request with retry on 5xx / network errors.

        Raises:
            GitHubAuthError: on 401.
            GitHubRateLimitError: on 403 with X-RateLimit-Remaining == 0.
            GitHubNotFoundError: on 404.
            GitHubServerError: when all retries on 5xx are exhausted.
            httpx.HTTPError: on other HTTP errors after retries.
        """
        last_exc: Exception | None = None

        for attempt in range(self._max_retries):
            t0 = time.monotonic()
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                )

                duration_ms = int((time.monotonic() - t0) * 1000)
                safe_hdrs = _redact_headers(dict(response.request.headers))
                logger.debug(
                    "github_api_request",
                    extra={
                        "method": method,
                        "url": str(response.request.url),
                        "status": response.status_code,
                        "duration_ms": duration_ms,
                        "attempt": attempt + 1,
                        "request_headers": safe_hdrs,
                    },
                )

                status = response.status_code

                if status == 401:
                    raise GitHubAuthError(f"GitHub returned 401 Unauthorized for {url}")

                if status == 403:
                    remaining = response.headers.get("X-RateLimit-Remaining", "")
                    if remaining == "0":
                        reset_epoch = int(response.headers.get("X-RateLimit-Reset", "0"))
                        raise GitHubRateLimitError(reset_epoch=reset_epoch)
                    # Other 403s (e.g. forbidden scope) — treat as auth error
                    raise GitHubAuthError(f"GitHub returned 403 Forbidden for {url}")

                if status == 404:
                    raise GitHubNotFoundError(f"GitHub returned 404 Not Found for {url}")

                if 500 <= status < 600:
                    last_exc = GitHubServerError(
                        f"GitHub returned {status} for {url} (attempt {attempt + 1})"
                    )
                    if attempt < self._max_retries - 1:
                        await sleep_backoff(attempt, self._backoff_min_sec, self._backoff_max_sec)
                    continue

                return response

            except (GitHubAuthError, GitHubNotFoundError, GitHubRateLimitError):
                raise
            except httpx.NetworkError as exc:
                last_exc = exc
                logger.warning(
                    "github_network_error",
                    extra={"url": url, "attempt": attempt + 1, "error": str(exc)},
                )
                if attempt < self._max_retries - 1:
                    await sleep_backoff(attempt, self._backoff_min_sec, self._backoff_max_sec)
                continue

        if isinstance(last_exc, GitHubServerError):
            raise last_exc
        if last_exc is not None:
            raise last_exc
        raise GitHubServerError(f"All {self._max_retries} attempts failed for {url}")

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def get_repo(self, owner: str, name: str) -> RepositoryDTO:
        """GET /repos/{owner}/{name} -> RepositoryDTO."""
        response = await self._request("GET", f"/repos/{owner}/{name}")
        return RepositoryDTO.model_validate(response.json())

    async def get_readme(self, owner: str, name: str, *, ref: str | None = None) -> str | None:
        """GET /repos/{owner}/{name}/readme with Accept: application/vnd.github.raw.

        Returns the raw markdown string, or None if the repo has no README (404).
        """
        headers = {"Accept": "application/vnd.github.raw"}
        params: dict[str, Any] = {}
        if ref is not None:
            params["ref"] = ref
        try:
            response = await self._request(
                "GET",
                f"/repos/{owner}/{name}/readme",
                headers=headers,
                params=params or None,
            )
        except GitHubNotFoundError:
            return None
        return response.text

    async def get_languages(self, owner: str, name: str) -> dict[str, int]:
        """GET /repos/{owner}/{name}/languages -> dict[language, bytes]."""
        response = await self._request("GET", f"/repos/{owner}/{name}/languages")
        dto = LanguagesDTO.model_validate(response.json())
        return dto.as_dict()

    async def list_starred(
        self,
        *,
        since: datetime | None = None,
        per_page: int = 100,
    ) -> AsyncIterator[StarredItem]:
        """GET /user/starred with Accept: application/vnd.github.star+json.

        Sorted by created desc (newest first). Paginates via Link header.
        If *since* is provided, stops yielding once starred_at < since.
        """
        return self._iter_starred(since=since, per_page=per_page)

    async def _iter_starred(
        self,
        *,
        since: datetime | None,
        per_page: int,
    ) -> AsyncIterator[StarredItem]:
        headers = {"Accept": "application/vnd.github.star+json"}
        params: dict[str, Any] = {
            "sort": "created",
            "direction": "desc",
            "per_page": per_page,
        }
        url: str | None = "/user/starred"
        first_page = True

        while url is not None:
            if first_page:
                response = await self._request("GET", url, headers=headers, params=params)
                first_page = False
            else:
                # url is an absolute URL from the Link header — bypass base_url composition
                response = await self._request_absolute(url, headers=headers)

            items: list[dict[str, Any]] = response.json()
            for raw in items:
                item = StarredItem.model_validate(raw)
                if since is not None and item.starred_at < since:
                    return
                yield item

            url = self._parse_next_link(response.headers.get("Link"))

    async def _request_absolute(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute a GET request to an absolute URL (pagination next links)."""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            t0 = time.monotonic()
            try:
                response = await self._client.get(url, headers=headers)
                duration_ms = int((time.monotonic() - t0) * 1000)
                safe_hdrs = _redact_headers(dict(response.request.headers))
                logger.debug(
                    "github_api_request",
                    extra={
                        "method": "GET",
                        "url": url,
                        "status": response.status_code,
                        "duration_ms": duration_ms,
                        "attempt": attempt + 1,
                        "request_headers": safe_hdrs,
                    },
                )

                status = response.status_code
                if status == 401:
                    raise GitHubAuthError(f"GitHub returned 401 for {url}")
                if status == 403:
                    remaining = response.headers.get("X-RateLimit-Remaining", "")
                    if remaining == "0":
                        reset_epoch = int(response.headers.get("X-RateLimit-Reset", "0"))
                        raise GitHubRateLimitError(reset_epoch=reset_epoch)
                    raise GitHubAuthError(f"GitHub returned 403 for {url}")
                if status == 404:
                    raise GitHubNotFoundError(f"GitHub returned 404 for {url}")
                if 500 <= status < 600:
                    last_exc = GitHubServerError(
                        f"GitHub returned {status} for {url} (attempt {attempt + 1})"
                    )
                    if attempt < self._max_retries - 1:
                        await sleep_backoff(attempt, self._backoff_min_sec, self._backoff_max_sec)
                    continue
                return response
            except (GitHubAuthError, GitHubNotFoundError, GitHubRateLimitError):
                raise
            except httpx.NetworkError as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await sleep_backoff(attempt, self._backoff_min_sec, self._backoff_max_sec)
                continue

        if last_exc is not None:
            raise last_exc
        raise GitHubServerError(f"All {self._max_retries} attempts failed for {url}")

    async def get_authenticated_user(self) -> AuthenticatedUserDTO:
        """GET /user -> AuthenticatedUserDTO."""
        response = await self._request("GET", "/user")
        return AuthenticatedUserDTO.model_validate(response.json())

    async def get_user_with_scopes(self) -> tuple[AuthenticatedUserDTO, list[str]]:
        """GET /user and return (user, scopes).

        Reads X-GitHub-OAuthScopes response header. GitHub omits this header for
        fine-grained PATs, so an empty list signals a fine-grained PAT.
        """
        response = await self._request("GET", "/user")
        user = AuthenticatedUserDTO.model_validate(response.json())
        raw = response.headers.get("X-GitHub-OAuthScopes", "").strip()
        if not raw:
            return user, []
        scopes = [s.strip() for s in raw.split(",") if s.strip()]
        return user, scopes

    async def list_gists(self, *, per_page: int = 100) -> list[GistDTO]:
        """GET /gists — return all gists for the authenticated user.

        Paginates via Link header using the same pattern as :meth:`list_starred`.
        Auth header is redacted in all log output.
        """
        params: dict[str, Any] = {"per_page": per_page}
        url: str | None = "/gists"
        first_page = True
        results: list[GistDTO] = []

        while url is not None:
            if first_page:
                response = await self._request("GET", url, params=params)
                first_page = False
            else:
                response = await self._request_absolute(url)

            items: list[dict[str, Any]] = response.json()
            for raw in items:
                results.append(GistDTO.model_validate(raw))

            url = self._parse_next_link(response.headers.get("Link"))

        return results

    async def list_owned_repos(self, *, per_page: int = 100) -> list[RepositoryDTO]:
        """GET /user/repos?affiliation=owner — return all repos owned by the authenticated user.

        Paginates via Link header using the same pattern as :meth:`list_gists`.
        """
        params: dict[str, Any] = {"affiliation": "owner", "per_page": per_page}
        url: str | None = "/user/repos"
        first_page = True
        results: list[RepositoryDTO] = []

        while url is not None:
            if first_page:
                response = await self._request("GET", url, params=params)
                first_page = False
            else:
                response = await self._request_absolute(url)

            items: list[dict[str, Any]] = response.json()
            for raw in items:
                results.append(RepositoryDTO.model_validate(raw))

            url = self._parse_next_link(response.headers.get("Link"))

        return results

    async def list_watched_repos(self, *, per_page: int = 100) -> list[RepositoryDTO]:
        """GET /user/subscriptions — return all repos watched by the authenticated user.

        Paginates via Link header using the same pattern as :meth:`list_gists`.
        """
        params: dict[str, Any] = {"per_page": per_page}
        url: str | None = "/user/subscriptions"
        first_page = True
        results: list[RepositoryDTO] = []

        while url is not None:
            if first_page:
                response = await self._request("GET", url, params=params)
                first_page = False
            else:
                response = await self._request_absolute(url)

            items: list[dict[str, Any]] = response.json()
            for raw in items:
                results.append(RepositoryDTO.model_validate(raw))

            url = self._parse_next_link(response.headers.get("Link"))

        return results

    async def probe_repository_access(self) -> bool:
        """GET /user/starred?per_page=1 to test repository-read capability.

        Returns True on 200, False on 403. Used for fine-grained PAT validation
        because scope names are opaque for those tokens.
        """
        try:
            await self._request("GET", "/user/starred", params={"per_page": "1"})
            return True
        except GitHubAuthError:
            return False
