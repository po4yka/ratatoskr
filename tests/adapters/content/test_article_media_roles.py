"""Tests for the role-aware image extractor in article_media.

The role filter exists to prevent Habr-class articles (one OG header image,
zero content-area images) from being routed to the slow vision model when
``article_vision_min_images`` would otherwise admit them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters.content.article_media import (
    _classify_image_role,
    extract_firecrawl_image_assets,
)


@dataclass
class _FakeCrawl:
    """Minimal stand-in for FirecrawlResult shape used by the extractor."""

    metadata_json: dict[str, Any] | None = None
    content_markdown: str | None = None


class TestClassifyImageRole:
    def test_og_image_source_key(self) -> None:
        assert _classify_image_role({"source_key": "og:image"}) == "header_og"
        assert _classify_image_role({"source_key": "ogImage"}) == "header_og"

    def test_markdown_image_is_content_area(self) -> None:
        assert _classify_image_role({"source_key": "markdown_image"}) == "content_area"

    def test_images_list_key_is_content_area(self) -> None:
        assert _classify_image_role({"source_key": "images"}) == "content_area"
        assert _classify_image_role({"source_key": "image_urls"}) == "content_area"

    def test_thumbnails_source_key(self) -> None:
        assert _classify_image_role({"source_key": "thumbnails"}) == "thumbnail"
        assert _classify_image_role({"source_key": "screenshots"}) == "thumbnail"

    def test_tiny_dimensions_classified_as_thumbnail(self) -> None:
        # Below both minimums -> thumbnail regardless of source_key
        candidate = {"source_key": "markdown_image", "width": 150, "height": 100}
        assert _classify_image_role(candidate) == "thumbnail"

    def test_one_dimension_above_threshold_keeps_content_area(self) -> None:
        # The thumbnail rule requires BOTH width and height below the threshold.
        candidate = {"source_key": "markdown_image", "width": 800, "height": 100}
        assert _classify_image_role(candidate) == "content_area"

    def test_unknown_source_falls_through(self) -> None:
        assert _classify_image_role({"source_key": "something_else"}) == "unknown"
        assert _classify_image_role({}) == "unknown"


class TestExtractRoleFilter:
    def test_og_only_article_keeps_og_when_no_content_area(self) -> None:
        # Habr-style: one OG header image, no inline content images.
        # Filter must NOT strip all images — that would deny vision routing
        # for legitimately image-light articles when the operator wants it.
        crawl = _FakeCrawl(
            metadata_json={"og:image": "https://habr.com/og-header.png"},
            content_markdown="",
        )
        assets, report = extract_firecrawl_image_assets(crawl, role_filter_enabled=True)

        assert len(assets) == 1
        assert assets[0].metadata["role"] == "header_og"
        assert report["role_filter_applied"] is False
        assert report["role_breakdown"] == {
            "header_og": 1,
            "content_area": 0,
            "thumbnail": 0,
            "unknown": 0,
        }

    def test_og_dropped_when_content_area_present(self) -> None:
        # The image-rich path: drop the OG header when an inline figure exists.
        crawl = _FakeCrawl(
            metadata_json={"og:image": "https://example.com/og.jpg"},
            content_markdown="![diagram](https://example.com/figure.png)",
        )
        assets, report = extract_firecrawl_image_assets(crawl, role_filter_enabled=True)

        assert len(assets) == 1
        assert assets[0].url == "https://example.com/figure.png"
        assert assets[0].metadata["role"] == "content_area"
        assert report["role_filter_applied"] is True
        assert report["rejected_reasons"].get("role_filter_decorative") == 1

    def test_role_filter_disabled_keeps_all(self) -> None:
        crawl = _FakeCrawl(
            metadata_json={"og:image": "https://example.com/og.jpg"},
            content_markdown="![diagram](https://example.com/figure.png)",
        )
        assets, report = extract_firecrawl_image_assets(crawl, role_filter_enabled=False)

        urls = sorted(asset.url for asset in assets)
        assert urls == [
            "https://example.com/figure.png",
            "https://example.com/og.jpg",
        ]
        assert report["role_filter_applied"] is False
        assert report["role_filter_enabled"] is False
        assert report["role_breakdown"]["header_og"] == 1
        assert report["role_breakdown"]["content_area"] == 1

    def test_thumbnail_dropped_with_content_area_present(self) -> None:
        # screenshots/thumbnails alongside content images: thumbnails go.
        crawl = _FakeCrawl(
            metadata_json={
                "screenshots": [
                    {"url": "https://example.com/thumb.jpg", "width": 120, "height": 90}
                ]
            },
            content_markdown="![figure](https://example.com/fig.png)",
        )
        assets, _report = extract_firecrawl_image_assets(crawl, role_filter_enabled=True)

        urls = {asset.url for asset in assets}
        assert urls == {"https://example.com/fig.png"}

    def test_report_breakdown_counts_all_roles(self) -> None:
        crawl = _FakeCrawl(
            metadata_json={
                "og:image": "https://example.com/og.jpg",
                "screenshots": ["https://example.com/thumb.png"],
                "images": ["https://example.com/photo1.jpg", "https://example.com/photo2.jpg"],
            },
        )
        _assets, report = extract_firecrawl_image_assets(crawl, role_filter_enabled=False)

        breakdown = report["role_breakdown"]
        assert breakdown["header_og"] == 1
        assert breakdown["thumbnail"] == 1
        assert breakdown["content_area"] == 2
