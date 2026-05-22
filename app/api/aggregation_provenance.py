"""Shared serializers for aggregation source provenance."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.api.models.responses import AggregationSourceBundle, AggregationSourceItem
from app.domain.models.source import SourceKind


def _safe_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text.startswith(("http://", "https://")):
        return None
    return text


def _metadata_value(*payloads: dict[str, Any], key: str) -> Any:
    for payload in payloads:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _document_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("normalized_document_json")
    return payload if isinstance(payload, dict) else {}


def _source_metadata(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("source_metadata_json")
    return payload if isinstance(payload, dict) else {}


def _extraction_metadata(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("extraction_metadata_json")
    return payload if isinstance(payload, dict) else {}


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).netloc or None
    except ValueError:
        return None


def source_item_from_record(
    record: dict[str, Any],
    *,
    fallback_session_id: int | None = None,
) -> AggregationSourceItem:
    document = _document_payload(record)
    document_metadata = (
        document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    )
    extraction_metadata = _extraction_metadata(record)
    source_metadata = _source_metadata(record)
    original_url = _safe_url(record.get("original_value"))
    normalized_url = _safe_url(record.get("normalized_value")) or original_url
    title = (
        str(
            _metadata_value(
                document,
                extraction_metadata,
                source_metadata,
                record,
                key="title",
            )
            or record.get("title_hint")
            or ""
        ).strip()
        or None
    )
    author = _metadata_value(document_metadata, extraction_metadata, source_metadata, key="author")
    published_at = _metadata_value(
        document_metadata,
        extraction_metadata,
        source_metadata,
        key="published_at",
    )
    domain = _metadata_value(
        document_metadata, extraction_metadata, source_metadata, key="domain"
    ) or _domain_from_url(normalized_url)
    deleted = bool(record.get("request_is_deleted") or record.get("summary_is_deleted"))
    return AggregationSourceItem(
        bundle_id=int(record.get("aggregation_session_id") or fallback_session_id or 0),
        source_item_id=str(record.get("source_item_id") or ""),
        item_id=record.get("id"),
        position=int(record.get("position") or 0),
        original_url=original_url,
        normalized_url=normalized_url,
        source_kind=str(record.get("source_kind") or SourceKind.UNKNOWN.value),
        extraction_status=str(record.get("status") or "unknown"),
        title=title,
        domain=str(domain).strip() if domain else None,
        author=str(author).strip() if author else None,
        published_at=str(published_at).strip() if published_at else None,
        error_code=record.get("failure_code"),
        error_message=record.get("failure_message"),
        request_id=record.get("request_id"),
        crawl_result_id=record.get("crawl_result_id"),
        summary_id=None if deleted else record.get("summary_id"),
        duplicate_of_item_id=record.get("duplicate_of_item_id"),
        deleted=deleted,
        metadata={
            key: value
            for key, value in {
                "contentSource": document_metadata.get("content_source")
                or extraction_metadata.get("content_source"),
                "extractionSource": (document.get("provenance") or {}).get("extraction_source")
                if isinstance(document.get("provenance"), dict)
                else None,
            }.items()
            if value not in (None, "")
        },
    )


def source_item_from_extraction_result(
    item: Any,
    *,
    session_id: int,
) -> AggregationSourceItem:
    document = item.normalized_document
    document_metadata = document.metadata if document is not None else {}
    original_url = _safe_url(document.provenance.original_value if document is not None else None)
    normalized_url = (
        _safe_url(document.provenance.normalized_value if document is not None else None)
        or original_url
    )
    failure = item.failure
    return AggregationSourceItem(
        bundle_id=session_id,
        source_item_id=item.source_item_id,
        item_id=item.item_id,
        position=item.position,
        original_url=original_url,
        normalized_url=normalized_url,
        source_kind=item.source_kind.value,
        extraction_status=item.status,
        title=document.title if document is not None else None,
        domain=str(
            document_metadata.get("domain") or _domain_from_url(normalized_url) or ""
        ).strip()
        or None,
        author=str(document_metadata.get("author") or "").strip() or None,
        published_at=str(document_metadata.get("published_at") or "").strip() or None,
        error_code=failure.code if failure is not None else None,
        error_message=failure.message if failure is not None else None,
        request_id=item.request_id,
        duplicate_of_item_id=item.duplicate_of_item_id,
        metadata={
            key: value
            for key, value in {
                "contentSource": document_metadata.get("content_source"),
                "extractionSource": document.provenance.extraction_source
                if document is not None
                else None,
            }.items()
            if value not in (None, "")
        },
    )


def build_source_bundle(
    *,
    session_id: int,
    correlation_id: str | None,
    status: str | None,
    persisted_items: list[dict[str, Any]] | None = None,
    extraction_items: list[Any] | None = None,
) -> AggregationSourceBundle:
    if persisted_items is not None:
        items = [
            source_item_from_record(item, fallback_session_id=session_id)
            for item in persisted_items
        ]
    else:
        items = [
            source_item_from_extraction_result(item, session_id=session_id)
            for item in extraction_items or []
        ]
    return AggregationSourceBundle(
        bundle_id=session_id,
        correlation_id=correlation_id,
        status=status,
        items=items,
    )
