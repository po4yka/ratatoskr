"""Public Atom feed endpoints for a user's saved summaries."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select

from app.api.dependencies.database import (
    get_auth_repository as resolve_auth_repository,
    get_session_manager as resolve_session_manager,
)
from app.api.models.responses import (
    UserFeedTokenRevocationSuccessResponse,
    UserFeedTokenSuccessResponse,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.core.time_utils import UTC, coerce_datetime
from app.db.models import Collection, CollectionItem, Request as StoredRequest, Summary

router = APIRouter()

RSS_FEED_CLIENT_ID = "rss_feed"
RSS_FEED_CACHE_CONTROL = "private, max-age=300"
ATOM_NS = "http://www.w3.org/2005/Atom"
ElementTree.register_namespace("", ATOM_NS)


@dataclass(frozen=True)
class FeedSummary:
    """Summary fields needed to render an Atom entry."""

    id: int
    title: str
    url: str | None
    text: str
    lang: str | None
    tags: tuple[str, ...]
    published_at: dt.datetime
    updated_at: dt.datetime


def get_feed_auth_repository() -> Any:
    """Resolve the auth repository without exposing factory kwargs as API params."""
    return resolve_auth_repository()


def get_feed_session_manager() -> Any:
    """Resolve the session manager without exposing factory kwargs as API params."""
    return resolve_session_manager()


def generate_feed_token() -> str:
    """Generate an unguessable feed token suitable for a URL query parameter."""
    return secrets.token_urlsafe(32)


def hash_feed_token(token: str, salt: str) -> str:
    """Hash a feed token for storage in ClientSecret."""
    return hmac.new(salt.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def new_feed_secret(token: str) -> tuple[str, str]:
    """Return ``(secret_hash, secret_salt)`` for a feed token."""
    salt = secrets.token_urlsafe(16)
    return hash_feed_token(token, salt), salt


@router.post("/me/feed-token", response_model=UserFeedTokenSuccessResponse)
async def rotate_user_library_feed_token(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    auth_repo: Any = Depends(get_feed_auth_repository),
) -> Any:
    """Rotate the current user's public-library feed token."""
    token = generate_feed_token()
    secret_hash, secret_salt = new_feed_secret(token)
    await auth_repo.async_replace_active_client_secret(
        user_id=user["user_id"],
        client_id=RSS_FEED_CLIENT_ID,
        secret_hash=secret_hash,
        secret_salt=secret_salt,
        label="Public library Atom feed",
        description="Token for GET /v1/users/me/feed.xml",
    )
    feed_url = f"{request.url_for('get_current_user_library_feed')}?token={quote(token)}"
    return success_response(
        {
            "token": token,
            "feedUrl": feed_url,
        }
    )


@router.delete("/me/feed-token", response_model=UserFeedTokenRevocationSuccessResponse)
async def revoke_user_library_feed_token(
    user: dict[str, Any] = Depends(get_current_user),
    auth_repo: Any = Depends(get_feed_auth_repository),
) -> Any:
    """Revoke the current user's active public-library feed token."""
    record = await auth_repo.async_get_client_secret(user["user_id"], RSS_FEED_CLIENT_ID)
    revoked = bool(record and record.get("status") == "active")
    if record and record.get("status") == "active":
        await auth_repo.async_update_client_secret(record["id"], status="revoked")
    return success_response({"revoked": revoked})


@router.get(
    "/me/feed.xml",
    name="get_current_user_library_feed",
    response_class=Response,
    openapi_extra={"security": []},
    responses={
        200: {
            "content": {"application/atom+xml": {}},
            "description": "Atom 1.0 feed of saved summaries.",
        },
        304: {"description": "Feed unchanged for the supplied ETag."},
        401: {"description": "Missing or invalid feed token."},
    },
)
async def get_current_user_library_feed(
    request: Request,
    token: Annotated[str, Query(min_length=16)],
    tag: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    collection: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    language: Literal["en", "ru"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    auth_repo: Any = Depends(get_feed_auth_repository),
    session_manager: Any = Depends(get_feed_session_manager),
) -> Response:
    """Return the token-scoped user's saved summaries as an Atom feed."""
    secret_record = await _find_active_feed_secret(auth_repo, token)
    if secret_record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid feed token",
        )

    user_id = int(secret_record.get("user_id") or secret_record.get("user"))
    items = await load_feed_summaries(
        session_manager,
        user_id=user_id,
        limit=limit,
        tag=tag,
        collection=collection,
        language=language,
    )
    feed_xml = build_atom_feed(
        user_id=user_id,
        items=items,
        self_url=str(request.url),
    )
    etag = build_feed_etag(feed_xml)
    headers = {
        "Cache-Control": RSS_FEED_CACHE_CONTROL,
        "ETag": etag,
    }
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    await auth_repo.async_update_client_secret(secret_record["id"], last_used_at=_now())
    return Response(
        content=feed_xml,
        media_type="application/atom+xml; charset=utf-8",
        headers=headers,
    )


async def _find_active_feed_secret(auth_repo: Any, token: str) -> dict[str, Any] | None:
    records = await auth_repo.async_list_client_secrets(
        client_id=RSS_FEED_CLIENT_ID,
        status="active",
    )
    for record in records:
        raw_expires_at = record.get("expires_at")
        expires_at = coerce_datetime(raw_expires_at) if raw_expires_at is not None else None
        if expires_at is not None and expires_at <= _now():
            continue
        expected = str(record.get("secret_hash") or "")
        actual = hash_feed_token(token, str(record.get("secret_salt") or ""))
        if hmac.compare_digest(actual, expected):
            return cast("dict[str, Any]", record)
    return None


async def load_feed_summaries(
    session_manager: Any,
    *,
    user_id: int,
    limit: int,
    tag: str | None,
    collection: str | None,
    language: Literal["en", "ru"] | None,
) -> list[FeedSummary]:
    """Load summaries visible to the token owner for Atom rendering."""
    fetch_limit = min(max(limit * 5, limit), 500) if tag else limit
    async with session_manager.session() as session:
        stmt = (
            select(Summary, StoredRequest)
            .join(StoredRequest, Summary.request_id == StoredRequest.id)
            .where(
                StoredRequest.user_id == user_id,
                Summary.is_deleted.is_(False),
                StoredRequest.is_deleted.is_(False),
            )
            .order_by(Summary.created_at.desc(), Summary.id.desc())
            .limit(fetch_limit)
        )
        if language is not None:
            stmt = stmt.where(Summary.lang == language)
        if collection is not None:
            stmt = (
                stmt.join(CollectionItem, CollectionItem.summary_id == Summary.id)
                .join(Collection, Collection.id == CollectionItem.collection_id)
                .where(
                    Collection.user_id == user_id,
                    Collection.is_deleted.is_(False),
                    _collection_filter(collection),
                )
            )
        rows = (await session.execute(stmt)).all()

    items: list[FeedSummary] = []
    for summary, stored_request in rows:
        item = _feed_summary_from_models(summary, stored_request)
        if tag is not None and tag.casefold() not in {value.casefold() for value in item.tags}:
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _collection_filter(collection: str) -> Any:
    if collection.isdecimal():
        return Collection.id == int(collection)
    return Collection.name == collection


def _feed_summary_from_models(summary: Summary, stored_request: StoredRequest) -> FeedSummary:
    payload = summary.json_payload if isinstance(summary.json_payload, dict) else {}
    tags = _tags_from(summary.topic_tags) or _tags_from(payload.get("topic_tags"))
    title = _first_text(
        summary.title,
        payload.get("title"),
        payload.get("tldr"),
        stored_request.normalized_url,
        stored_request.input_url,
        default=f"Summary {summary.id}",
    )
    text = _first_text(
        payload.get("summary_250"),
        payload.get("tldr"),
        payload.get("summary_1000"),
        default="",
    )
    published_at = coerce_datetime(stored_request.created_at) or coerce_datetime(summary.created_at)
    updated_at = coerce_datetime(summary.updated_at) or published_at or _epoch()
    return FeedSummary(
        id=summary.id,
        title=title,
        url=stored_request.normalized_url or stored_request.input_url,
        text=text,
        lang=summary.lang,
        tags=tuple(tags),
        published_at=published_at or _epoch(),
        updated_at=updated_at,
    )


def _tags_from(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _first_text(*values: Any, default: str) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def build_atom_feed(*, user_id: int, items: list[FeedSummary], self_url: str) -> bytes:
    """Build an Atom 1.0 feed document."""
    feed = ElementTree.Element(f"{{{ATOM_NS}}}feed")
    ElementTree.SubElement(feed, f"{{{ATOM_NS}}}title").text = "Ratatoskr saved summaries"
    ElementTree.SubElement(feed, f"{{{ATOM_NS}}}id").text = f"tag:ratatoskr.local,2026:user-{user_id}-library"
    self_link = ElementTree.SubElement(feed, f"{{{ATOM_NS}}}link")
    self_link.set("rel", "self")
    self_link.set("href", self_url)
    latest_updated = max((item.updated_at for item in items), default=_epoch())
    ElementTree.SubElement(feed, f"{{{ATOM_NS}}}updated").text = _atom_datetime(latest_updated)

    for item in items:
        entry = ElementTree.SubElement(feed, f"{{{ATOM_NS}}}entry")
        ElementTree.SubElement(entry, f"{{{ATOM_NS}}}title").text = item.title
        ElementTree.SubElement(entry, f"{{{ATOM_NS}}}id").text = f"tag:ratatoskr.local,2026:summary-{item.id}"
        if item.url:
            link = ElementTree.SubElement(entry, f"{{{ATOM_NS}}}link")
            link.set("href", item.url)
        ElementTree.SubElement(entry, f"{{{ATOM_NS}}}published").text = _atom_datetime(
            item.published_at
        )
        ElementTree.SubElement(entry, f"{{{ATOM_NS}}}updated").text = _atom_datetime(
            item.updated_at
        )
        if item.lang:
            entry.set("{http://www.w3.org/XML/1998/namespace}lang", item.lang)
        for tag in item.tags:
            category = ElementTree.SubElement(entry, f"{{{ATOM_NS}}}category")
            category.set("term", tag)
        summary = ElementTree.SubElement(entry, f"{{{ATOM_NS}}}summary")
        summary.set("type", "text")
        summary.text = item.text

    return cast("bytes", ElementTree.tostring(feed, encoding="utf-8", xml_declaration=True))


def build_feed_etag(feed_xml: bytes) -> str:
    """Return the strong ETag for a feed payload."""
    return f'"{hashlib.sha256(feed_xml).hexdigest()}"'


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Return whether an If-None-Match header matches the generated ETag."""
    if not if_none_match:
        return False
    values = [value.strip() for value in if_none_match.split(",")]
    return "*" in values or etag in values or etag.strip('"') in values


def _atom_datetime(value: dt.datetime) -> str:
    normalized = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def _epoch() -> dt.datetime:
    return dt.datetime(1970, 1, 1, tzinfo=UTC)


def _now() -> dt.datetime:
    return dt.datetime.now(UTC)
