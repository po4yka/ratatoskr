"""Claude.ai (Anthropic) account backup client.

Drives the undocumented internal web API through an already-authenticated browser
session (sessionKey cookie carried by the CloakBrowser context). Endpoint shapes
were extracted from the verified OSS exporters (agoramachina/claude-exporter,
socketteer, twilligon); see TODO(live-validation).
"""

from __future__ import annotations

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

_API = "https://claude.ai"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_ARTIFACT_TAG_RE = re.compile(
    r"<antArtifact\s+(?P<attrs>[^>]*)>(?P<content>.*?)</antArtifact>", re.DOTALL
)
_ATTR_RE = re.compile(r'(\w+)=["\']([^"\']*)["\']')

# TODO(live-validation): Claude.ai internal API shapes extracted from OSS
# exporters; live validation against a real account is required. Project
# knowledge-base file downloads and the Enterprise Compliance API path
# (AI_BACKUP_CLAUDE_COMPLIANCE_KEY) are intentionally not implemented in P1.


def _should_skip(updated_at: str | None, since: dt.datetime | None) -> bool:
    if since is None or not updated_at:
        return False
    try:
        ts = dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    ts = ts.astimezone(dt.UTC) if ts.tzinfo else ts.replace(tzinfo=dt.UTC)
    return ts <= since


class ClaudeClient:
    """Backs up a Claude.ai account via its internal web API."""

    def __init__(
        self,
        fetcher: AuthedFetcher,
        writer: AiBackupDiskWriter,
        *,
        download_files: bool = True,
        incremental: bool = True,
        last_backed_up_at: dt.datetime | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._writer = writer
        self._download_files = download_files
        self._incremental = incremental
        self._since = last_backed_up_at
        self._org_id: str | None = None
        self.skipped = 0
        self.resumed = 0

    # -- HTTP plumbing ------------------------------------------------------

    @staticmethod
    def _check_auth(resp: FetchResponse, url: str) -> None:
        # Only 401/403 are terminal (auth gone). 429 + 5xx are TRANSIENT and must
        # NOT halt the service — they record a retryable failure with backoff.
        # (A redirect to login is followed by the fetcher and surfaces as an HTML
        # 200, caught by _parse_json's interstitial check.)
        if resp.status in (401, 403):
            raise AiBackupAuthExpiredError(f"Claude session rejected: HTTP {resp.status} on {url}")
        if resp.status == 429:
            raise AiBackupMaxRequestsError(f"Claude rate-limited: HTTP 429 on {url}")
        if resp.status >= 500:
            raise AiBackupError(f"Claude server error: HTTP {resp.status} on {url}")

    def _parse_json(self, resp: FetchResponse, url: str) -> Any:
        body = resp.bytes()
        if body[:14].lower().startswith(b"<!doctype") or body[:5].lower() == b"<html":
            raise AiBackupAuthExpiredError(f"Cloudflare interstitial served as 200 on {url}")
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AiBackupParseError(f"JSON parse failed on {url}: {exc}") from exc

    async def _get(self, url: str) -> Any:
        headers = {"Accept": "application/json", "User-Agent": _UA}
        try:
            resp = await self._fetcher.get(url, headers=headers)
        except RequestCapExceededError as exc:
            raise AiBackupMaxRequestsError(str(exc)) from exc
        self._check_auth(resp, url)
        return self._parse_json(resp, url)

    # -- Collection ---------------------------------------------------------

    async def collect(self) -> dict[str, int]:
        counts = {"conversations": 0, "projects": 0, "files": 0, "artifacts": 0}
        org = quote(await self._get_org_id(), safe="")

        try:
            for project in await self._get(f"{_API}/api/organizations/{org}/projects"):
                pid = self._uuid(project)
                if pid:
                    self._writer.write_project(pid, project)
                    counts["projects"] += 1
        except (AiBackupAuthExpiredError, AiBackupMaxRequestsError):
            raise
        except Exception as exc:
            logger.warning("claude_projects_failed", extra={"error": str(exc)})

        seen: set[str] = set()
        conversations = await self._get(f"{_API}/api/organizations/{org}/chat_conversations")
        for conv in conversations if isinstance(conversations, list) else []:
            uuid = conv.get("uuid")
            if not uuid or uuid in seen:
                continue
            seen.add(uuid)
            if self._incremental and _should_skip(conv.get("updated_at"), self._since):
                self.skipped += 1
                continue
            saved = self._writer.load_saved_conversation(uuid)
            if saved is not None:
                # Resume: already on disk from a prior interrupted run today.
                counts["conversations"] += 1
                counts["artifacts"] += self._write_artifacts(saved, uuid)
                self.resumed += 1
                continue
            detail = await self._get(
                f"{_API}/api/organizations/{org}/chat_conversations/{quote(uuid, safe='')}"
                "?tree=True&rendering_mode=messages&render_all_tools=true"
            )
            self._writer.write_conversation(uuid, detail)
            counts["conversations"] += 1
            counts["artifacts"] += self._write_artifacts(detail, uuid)
        return counts

    async def _get_org_id(self) -> str:
        if self._org_id:
            return self._org_id
        # Preferred: /api/account memberships flagged with the 'chat' capability.
        try:
            account = await self._get(f"{_API}/api/account")
            for m in (account or {}).get("memberships", []) if isinstance(account, dict) else []:
                org = m.get("organization") or {}
                if "chat" in (org.get("capabilities") or []):
                    self._org_id = org.get("uuid")
                    if self._org_id:
                        return self._org_id
        except (AiBackupAuthExpiredError, AiBackupMaxRequestsError):
            raise
        except Exception as exc:
            logger.debug("claude_account_probe_failed", extra={"error": str(exc)})
        orgs = await self._get(f"{_API}/api/organizations")
        org_list = orgs if isinstance(orgs, list) else []
        for org in org_list:
            if "chat" in (org.get("capabilities") or []):
                self._org_id = org.get("uuid")
                if self._org_id:
                    return self._org_id
        if org_list:
            self._org_id = org_list[0].get("uuid")
        if not self._org_id:
            raise AiBackupAuthExpiredError("Claude returned no organizations (logged out)")
        return self._org_id

    @staticmethod
    def _uuid(obj: dict[str, Any]) -> str | None:
        return obj.get("uuid") or obj.get("project_uuid") or obj.get("projectUuid") or obj.get("id")

    def _write_artifacts(self, detail: dict[str, Any], conv_id: str) -> int:
        written = 0
        messages = detail.get("chat_messages", []) if isinstance(detail, dict) else []
        for message in messages:
            for art in self._extract_artifacts(message):
                ext = art.get("language") or _ext_for_content_type(art.get("content_type", ""))
                code = art.get("code")
                data = (code if isinstance(code, str) else "").encode("utf-8")
                self._writer.write_artifact(conv_id, art["artifact_id"], ext, data)
                written += 1
        return written

    @staticmethod
    def _extract_artifacts(message: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            # New format: tool_use block with display_content.
            if block.get("type") == "tool_use" and block.get("name") in (
                "artifacts",
                "create_file",
            ):
                dc = block.get("display_content") or {}
                code = dc.get("code", "")
                if dc.get("type") == "json_block" and isinstance(code, str):
                    try:
                        parsed = json.loads(code)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict) and isinstance(parsed.get("code"), str):
                        code = parsed["code"]
                results.append(
                    {
                        "artifact_id": block.get("id") or f"artifact_{len(results)}",
                        "content_type": dc.get("type", "unknown"),
                        "language": dc.get("language", ""),
                        "code": code,
                    }
                )
            # Old format: <antArtifact ...> embedded in message text.
            text = block.get("text") or ""
            for m in _ARTIFACT_TAG_RE.finditer(text):
                attrs = dict(_ATTR_RE.findall(m.group("attrs")))
                results.append(
                    {
                        "artifact_id": attrs.get("identifier") or f"old_artifact_{len(results)}",
                        "content_type": attrs.get("type", "unknown"),
                        "language": attrs.get("language", ""),
                        "code": m.group("content"),
                    }
                )
        return results


def _ext_for_content_type(content_type: str) -> str:
    mapping = {
        "text/markdown": "md",
        "text/html": "html",
        "application/vnd.ant.code": "txt",
        "image/svg+xml": "svg",
        "application/vnd.ant.mermaid": "mmd",
    }
    return mapping.get(content_type, "txt")


__all__ = ["ClaudeClient"]
