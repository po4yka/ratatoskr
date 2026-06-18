"""T7: ContentExtractionAdapter (ExtractionPort) -- wraps the pure extraction path.

CI-safe: no network / no DB. The underlying ``ContentExtractor.extract_content_pure``
and the request repo are faked, so the adapter's mapping + lang-persistence +
failure-propagation contract is exercised in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.content.extraction_adapter import ContentExtractionAdapter
from app.application.ports.extraction import ExtractionPort, ExtractionRequest, ExtractionResult
from app.core.url_utils import normalize_url, url_hash_sha256


class _FakeContentExtractor:
    def __init__(
        self,
        *,
        result: tuple[str, str, dict[str, Any]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._result = result or (
            "body text",
            "markdown",
            {"detected_lang": "en", "firecrawl_metadata": {"title": "Headline"}},
        )
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def extract_content_pure(
        self, url: str, correlation_id: str | None = None, request_id: int | None = None
    ) -> tuple[str, str, dict[str, Any]]:
        self.calls.append({"url": url, "correlation_id": correlation_id, "request_id": request_id})
        if self._raises is not None:
            raise self._raises
        return self._result


class _FakeRequestRepo:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises
        self.lang_updates: list[tuple[int, str]] = []

    async def async_update_request_lang_detected(self, request_id: int, lang: str) -> None:
        if self._raises:
            raise RuntimeError("db down")
        self.lang_updates.append((request_id, lang))


def _adapter(extractor: Any, repo: Any) -> ContentExtractionAdapter:
    return ContentExtractionAdapter(content_extractor=extractor, request_repo=repo)


async def test_adapter_satisfies_extraction_port() -> None:
    adapter = _adapter(_FakeContentExtractor(), _FakeRequestRepo())
    assert isinstance(adapter, ExtractionPort)  # @runtime_checkable structural match


async def test_extract_maps_pure_result_to_result_and_persists_lang() -> None:
    extractor = _FakeContentExtractor()
    repo = _FakeRequestRepo()
    adapter = _adapter(extractor, repo)

    result = await adapter.extract(
        ExtractionRequest(url="https://example.com/post", request_id=7, correlation_id="cid-9")
    )

    assert isinstance(result, ExtractionResult)
    assert result.request_id == 7
    assert result.content_text == "body text"
    assert result.content_source == "markdown"
    assert result.detected_lang == "en"
    assert result.title == "Headline"
    assert result.dedupe_hash  # sha256 of normalized url, non-empty
    # pure path routed with the existing request_id + correlation id.
    assert extractor.calls[0]["request_id"] == 7
    assert extractor.calls[0]["correlation_id"] == "cid-9"
    # detected language persisted against the request row (interactive-path parity).
    assert repo.lang_updates == [(7, "en")]


async def test_extract_uses_canonical_url_for_dedupe_hash() -> None:
    extractor = _FakeContentExtractor(
        result=(
            "body",
            "markdown",
            {
                "detected_lang": "en",
                "canonical_url": "https://example.com/canonical?utm_source=newsletter",
            },
        )
    )
    result = await _adapter(extractor, _FakeRequestRepo()).extract(
        ExtractionRequest(url="https://short.example/r?id=1", request_id=7)
    )

    expected_canonical = normalize_url("https://example.com/canonical?utm_source=newsletter")
    assert result.canonical_url == expected_canonical
    assert result.dedupe_hash == url_hash_sha256(expected_canonical)
    assert result.metadata["canonical_url"] == expected_canonical


async def test_extract_title_falls_back_to_normalized_document() -> None:
    extractor = _FakeContentExtractor(
        result=(
            "text",
            "markdown",
            {"detected_lang": "en", "normalized_source_document": {"title": "NSD Title"}},
        )
    )
    result = await _adapter(extractor, _FakeRequestRepo()).extract(
        ExtractionRequest(url="https://example.com", request_id=1)
    )
    assert result.title == "NSD Title"


async def test_extract_detects_lang_when_metadata_missing() -> None:
    # No detected_lang in metadata -> adapter falls back to detect_language(text).
    extractor = _FakeContentExtractor(result=("Hello world, this is English text.", "markdown", {}))
    result = await _adapter(extractor, _FakeRequestRepo()).extract(
        ExtractionRequest(url="https://example.com", request_id=1)
    )
    assert isinstance(result.detected_lang, str) and result.detected_lang


async def test_extract_lang_persist_failure_is_swallowed() -> None:
    # A lang-persist failure must not fail extraction (best-effort).
    extractor = _FakeContentExtractor()
    result = await _adapter(extractor, _FakeRequestRepo(raises=True)).extract(
        ExtractionRequest(url="https://example.com", request_id=1)
    )
    assert result.content_text == "body text"


async def test_extract_propagates_extraction_failure() -> None:
    # extract_content_pure raises ValueError on a (persisted) extraction failure;
    # the adapter must let it propagate to the terminal-failure path.
    extractor = _FakeContentExtractor(raises=ValueError("Extraction failed: boom"))
    with pytest.raises(ValueError, match="Extraction failed"):
        await _adapter(extractor, _FakeRequestRepo()).extract(
            ExtractionRequest(url="https://example.com", request_id=1)
        )


async def test_extract_skips_lang_persist_when_no_request_id() -> None:
    extractor = _FakeContentExtractor()
    repo = _FakeRequestRepo()
    await _adapter(extractor, repo).extract(ExtractionRequest(url="https://example.com"))
    assert repo.lang_updates == []  # nothing to attach lang to


# --------------------------------------------------------------------------- #
# Article-vision image lifting (audit #2): the adapter projects the quality-/
# role-filtered image URLs from the normalized source document's ``media`` list so
# the graph build_prompt node can route image-rich articles to the vision model.
# Before the fix the adapter hardcoded ``images=[]`` and the path was dead.
# --------------------------------------------------------------------------- #


async def test_extract_lifts_image_urls_from_nsd_media() -> None:
    extractor = _FakeContentExtractor(
        result=(
            "text",
            "markdown",
            {
                "detected_lang": "en",
                "normalized_source_document": {
                    "title": "T",
                    "media": [
                        {"kind": "image", "url": "https://cdn.example.com/a.jpg"},
                        {"kind": "image", "url": "https://cdn.example.com/b.jpg"},
                    ],
                },
            },
        )
    )
    result = await _adapter(extractor, _FakeRequestRepo()).extract(
        ExtractionRequest(url="https://example.com", request_id=1)
    )
    assert result.images == [
        "https://cdn.example.com/a.jpg",
        "https://cdn.example.com/b.jpg",
    ]


async def test_extract_images_empty_when_no_media() -> None:
    # No NSD / no media -> empty image list (sources without images stay text-only).
    extractor = _FakeContentExtractor(result=("text", "markdown", {"detected_lang": "en"}))
    result = await _adapter(extractor, _FakeRequestRepo()).extract(
        ExtractionRequest(url="https://example.com", request_id=1)
    )
    assert result.images == []


async def test_extract_images_skip_media_entries_without_url() -> None:
    extractor = _FakeContentExtractor(
        result=(
            "text",
            "markdown",
            {
                "detected_lang": "en",
                "normalized_source_document": {
                    "media": [
                        {"kind": "image", "url": "   "},  # blank -> dropped
                        {"kind": "image"},  # no url -> dropped
                        {"kind": "image", "url": "https://cdn.example.com/c.jpg"},
                    ]
                },
            },
        )
    )
    result = await _adapter(extractor, _FakeRequestRepo()).extract(
        ExtractionRequest(url="https://example.com", request_id=1)
    )
    assert result.images == ["https://cdn.example.com/c.jpg"]
