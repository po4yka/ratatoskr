"""ChatGPT (chatgpt.com) account backup client.

Drives the undocumented internal web API through an already-authenticated browser
session (``AuthedFetcher`` over a CloakBrowser context). Endpoint shapes were
extracted from the verified OSS exporters (brianjlacy/export-chatgpt,
pionxzh/chatgpt-exporter); see TODO(live-validation).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from app.adapters.ai_backup.errors import (
    AiBackupAuthExpiredError,
    AiBackupError,
    AiBackupMaxRequestsError,
    AiBackupParseError,
)
from app.adapters.content.browser_auth.authenticated_context import (
    FetchResponse,
    RequestCapExceededError,
)
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.ai_backup.disk_writer import AiBackupDiskWriter
    from app.adapters.content.browser_auth.authenticated_context import AuthedFetcher

logger = get_logger(__name__)

_API = "https://chatgpt.com"
_PAGE_SIZE = 28
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
# Strip either asset-pointer scheme before treating the remainder as a file id.
_ASSET_POINTER_RE = re.compile(r"^(sediment|file-service)://")

# TODO(live-validation): the ChatGPT internal API is undocumented and may change
# without notice; live validation against a real account is required before this
# is production-ready. Deep-research SSE streams and adaptive 429 throttling are
# intentionally not implemented in P1 (the final report is already in the
# conversation mapping; fixed delay comes from AI_BACKUP_REQUEST_DELAY_MS).


def _should_skip(update_time: float | str | None, since: dt.datetime | None) -> bool:
    """True when a conversation has not changed since the last successful run."""
    if since is None or update_time is None:
        return False
    if isinstance(update_time, (int, float)):
        ts = dt.datetime.fromtimestamp(update_time, tz=dt.UTC)
    else:
        try:
            ts = dt.datetime.fromisoformat(update_time)
        except ValueError:
            return False
        ts = ts.astimezone(dt.UTC) if ts.tzinfo else ts.replace(tzinfo=dt.UTC)
    return ts <= since


class ChatGptClient:
    """Backs up a ChatGPT account via its internal web API."""

    def __init__(
        self,
        fetcher: AuthedFetcher,
        writer: AiBackupDiskWriter,
        *,
        bearer_token: str | None = None,
        download_files: bool = True,
        incremental: bool = True,
        last_backed_up_at: dt.datetime | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._writer = writer
        self._access_token: str | None = bearer_token
        self._account_id: str | None = None
        self._download_files = download_files
        self._incremental = incremental
        self._since = last_backed_up_at
        self.skipped = 0
        self.resumed = 0

    # -- HTTP plumbing ------------------------------------------------------

    def _make_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "User-Agent": _UA,
        }
        if self._account_id:  # Teams/Enterprise only
            headers["chatgpt-account-id"] = self._account_id
        return headers

    @staticmethod
    def _check_response(resp: FetchResponse, url: str) -> None:
        if resp.status in (401, 403):
            raise AiBackupAuthExpiredError(f"ChatGPT session expired: HTTP {resp.status} on {url}")
        if resp.status == 429:
            raise AiBackupMaxRequestsError(f"ChatGPT rate-limited: HTTP 429 on {url}")
        if resp.status >= 400:
            raise AiBackupError(f"ChatGPT request failed: HTTP {resp.status} on {url}")

    async def _get(self, url: str, *, auth: bool = True) -> FetchResponse:
        headers = (
            self._make_headers() if auth else {"User-Agent": _UA, "Accept": "application/json"}
        )
        try:
            resp = await self._fetcher.get(url, headers=headers)
        except RequestCapExceededError as exc:
            raise AiBackupMaxRequestsError(str(exc)) from exc
        self._check_response(resp, url)
        return resp

    @staticmethod
    def _json_object(resp: FetchResponse, url: str) -> dict[str, Any]:
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AiBackupParseError(f"ChatGPT returned invalid JSON on {url}: {exc}") from exc
        if not isinstance(data, dict):
            raise AiBackupParseError(f"ChatGPT returned a non-object response on {url}")
        return data

    # -- Auth ---------------------------------------------------------------

    async def exchange_session_cookie(self) -> None:
        """Fetch the short-lived access token from ``/api/auth/session``.

        The session cookie is carried automatically by the browser context.
        Decodes the JWT to extract the Teams/Enterprise account id when present.
        """
        resp = await self._get(f"{_API}/api/auth/session", auth=False)
        try:
            data = self._json_object(resp, f"{_API}/api/auth/session")
        except AiBackupParseError as exc:
            raise AiBackupAuthExpiredError(str(exc)) from exc
        token = data.get("accessToken")
        if not token:
            raise AiBackupAuthExpiredError("ChatGPT session has no accessToken (logged out)")
        self._access_token = token
        self._account_id = self._extract_account_id(token)

    @staticmethod
    def _extract_account_id(token: str) -> str | None:
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
            return None
        auth = payload.get("https://api.openai.com/auth", {}) if isinstance(payload, dict) else {}
        plan = auth.get("chatgpt_plan_type")
        if plan in ("team", "enterprise"):
            return auth.get("chatgpt_account_id")
        return None

    # -- Collection ---------------------------------------------------------

    async def collect(self) -> dict[str, int]:
        counts = {"conversations": 0, "projects": 0, "files": 0, "artifacts": 0}
        if not self._access_token:
            await self.exchange_session_cookie()

        conv_index: dict[str, dict[str, Any]] = {}
        for is_archived in (False, True):
            await self._list_conversations(conv_index, is_archived=is_archived)

        await self._collect_projects(conv_index, counts)

        file_refs: dict[str, str] = {}
        for conv_id, meta in conv_index.items():
            if self._incremental and _should_skip(meta.get("update_time"), self._since):
                self.skipped += 1
                continue
            saved = self._writer.load_saved_conversation(conv_id)
            listed_update = meta.get("update_time")
            if (
                saved is not None
                and listed_update is not None
                and saved.get("update_time") == listed_update
            ):
                # Already on disk from a prior (possibly interrupted) run today:
                # resume only when it is the same provider version. A later
                # same-day update must replace the saved detail.
                counts["conversations"] += 1
                self._collect_file_refs(saved, conv_id, file_refs)
                self.resumed += 1
                continue
            detail = await self._get_conversation(conv_id)
            await asyncio.to_thread(self._writer.write_conversation, conv_id, detail)
            counts["conversations"] += 1
            self._collect_file_refs(detail, conv_id, file_refs)

        if self._download_files and file_refs:
            await self._download_all(file_refs, counts)

        return counts

    async def _list_conversations(
        self, conv_index: dict[str, dict[str, Any]], *, is_archived: bool
    ) -> None:
        offset = 0
        consecutive_no_new = 0
        while True:
            url = (
                f"{_API}/backend-api/conversations?offset={offset}&limit={_PAGE_SIZE}"
                f"&order=updated&is_archived={str(is_archived).lower()}"
            )
            data = self._json_object(await self._get(url), url)
            items = data.get("items")
            if not isinstance(items, list):
                raise AiBackupParseError(f"ChatGPT conversation list has no items array on {url}")
            new_this_page = 0
            for c in items:
                cid = c.get("id")
                if cid and cid not in conv_index:
                    conv_index[cid] = {**c, "_archived": is_archived}
                    new_this_page += 1
            offset += len(items)
            if new_this_page == 0:
                consecutive_no_new += 1
                if consecutive_no_new >= 3:
                    break
            else:
                consecutive_no_new = 0
            if len(items) < _PAGE_SIZE:
                break

    async def _get_conversation(self, conv_id: str) -> dict[str, Any]:
        url = f"{_API}/backend-api/conversation/{quote(conv_id, safe='')}"
        return self._json_object(await self._get(url), url)

    async def _collect_projects(
        self, conv_index: dict[str, dict[str, Any]], counts: dict[str, int]
    ) -> None:
        cursor: str | None = None
        while True:
            url = f"{_API}/backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=0"
            if cursor:
                url += f"&cursor={quote(cursor, safe='')}"
            data = self._json_object(await self._get(url), url)
            items = data.get("items")
            if not isinstance(items, list):
                raise AiBackupParseError(f"ChatGPT project list has no items array on {url}")
            for item in items:
                if not isinstance(item, dict):
                    raise AiBackupParseError(f"ChatGPT project list contains a non-object on {url}")
                inner = item.get("gizmo") or {}
                gizmo = inner.get("gizmo") or inner
                gid = gizmo.get("id") if isinstance(gizmo, dict) else None
                if not gid:
                    continue
                await asyncio.to_thread(self._writer.write_project, gid, gizmo)
                counts["projects"] += 1
                await self._list_project_conversations(gid, conv_index)
            cursor = data.get("cursor") if isinstance(data, dict) else None
            if not cursor:
                break

    async def _list_project_conversations(
        self, gizmo_id: str, conv_index: dict[str, dict[str, Any]]
    ) -> None:
        cursor = "0"
        while True:
            url = (
                f"{_API}/backend-api/gizmos/{quote(gizmo_id, safe='')}"
                f"/conversations?cursor={quote(cursor, safe='')}"
            )
            data = self._json_object(await self._get(url), url)
            items = data.get("items")
            if not isinstance(items, list):
                raise AiBackupParseError(
                    f"ChatGPT project conversation list has no items array on {url}"
                )
            for c in items:
                if not isinstance(c, dict):
                    raise AiBackupParseError(
                        f"ChatGPT project conversation list contains a non-object on {url}"
                    )
                cid = c.get("id")
                if cid and cid not in conv_index:
                    conv_index[cid] = {**c, "_gizmo_id": gizmo_id}
            cursor = data.get("cursor") if isinstance(data, dict) else None
            if not cursor:
                break

    @staticmethod
    def _collect_file_refs(detail: dict[str, Any], conv_id: str, file_refs: dict[str, str]) -> None:
        mapping = detail.get("mapping", {}) if isinstance(detail, dict) else {}
        for node in mapping.values():
            msg = (node or {}).get("message") or {}
            content = msg.get("content") or {}
            if content.get("content_type") != "multimodal_text":
                continue
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                ptr = part.get("asset_pointer")
                if ptr:
                    file_id = _ASSET_POINTER_RE.sub("", ptr)
                    file_refs.setdefault(file_id, conv_id)

    async def _download_all(self, file_refs: dict[str, str], counts: dict[str, int]) -> None:
        for file_id, conv_id in file_refs.items():
            url = (
                f"{_API}/backend-api/files/download/{quote(file_id, safe='')}"
                f"?conversation_id={quote(conv_id, safe='')}&inline=false"
            )
            meta = self._json_object(await self._get(url), url)
            if meta.get("status") != "success":
                raise AiBackupError(f"ChatGPT file metadata request was not successful on {url}")
            download_url = meta.get("download_url")
            if not isinstance(download_url, str) or not download_url:
                raise AiBackupParseError(f"ChatGPT file metadata has no download_url on {url}")
            # Signed CDN URL: access is by query-param signature, not the Bearer
            # token -- do NOT forward Authorization to oaiusercontent.com.
            resp = await self._get(download_url, auth=False)
            await asyncio.to_thread(
                self._writer.write_file, file_id, meta.get("file_name") or file_id, resp.bytes()
            )
            counts["files"] += 1


__all__ = ["ChatGptClient"]
