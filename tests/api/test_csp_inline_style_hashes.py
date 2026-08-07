"""The SPA's inline <style> blocks must survive style-src.

ratatoskr-web's index.html carries an anti-flash <style> block that paints the
html background before app CSS loads. It is an element, not a `style=` attribute,
so `style-src-attr 'unsafe-inline'` never covered it and `style-src 'self'`
dropped it -- silently, since a blocked style throws nothing. The white flash it
exists to prevent happened on every cold load in dark mode.

The hashes are derived from the served index.html rather than pinned, so a
frontend bundle bump cannot reintroduce the same silent breakage.
"""

from __future__ import annotations

import base64
import hashlib
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import middleware
from app.api.middleware import (
    _app_csp,
    _spa_inline_style_hashes,
    security_headers_middleware,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "ops/docker/ratatoskr-web.bundle.tar.gz"


@pytest.fixture(autouse=True)
def _clear_hash_cache() -> Iterator[None]:
    """The lookup is cached for the process; each test needs its own index.html."""
    _spa_inline_style_hashes.cache_clear()
    yield
    _spa_inline_style_hashes.cache_clear()


def _point_at(monkeypatch: pytest.MonkeyPatch, html: str | None, tmp_path: Path) -> None:
    target = tmp_path / "index.html"
    if html is not None:
        target.write_text(html, encoding="utf-8")
    monkeypatch.setattr(middleware, "_SPA_INDEX_HTML", target)


def test_hash_is_over_the_block_content_not_the_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expected value computed independently -- hashing the tag or the whole file fails here."""
    _point_at(
        monkeypatch, "<html><head><style>html{background:#f5f5f5}</style></head></html>", tmp_path
    )

    assert _spa_inline_style_hashes() == ("'sha256-HZ75q+QO5LDUVc97U/plZzTbLcIxmPbaWWb0+YM2DQg='",)


def test_every_block_is_hashed_in_document_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_at(
        monkeypatch,
        "<style>html{background:#f5f5f5}</style><style>body{color:red}</style>",
        tmp_path,
    )

    assert _spa_inline_style_hashes() == (
        "'sha256-HZ75q+QO5LDUVc97U/plZzTbLcIxmPbaWWb0+YM2DQg='",
        "'sha256-FcQqt3aNlV7AZnGV4zkQRVeCeJOxbMPnQSx258L803E='",
    )


def test_style_tag_with_attributes_is_still_matched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_at(monkeypatch, '<style type="text/css">html{background:#f5f5f5}</style>', tmp_path)

    assert _spa_inline_style_hashes() == ("'sha256-HZ75q+QO5LDUVc97U/plZzTbLcIxmPbaWWb0+YM2DQg='",)


def test_no_hashes_when_the_spa_is_not_staged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bare API or test run serves no index.html and must not break on that."""
    _point_at(monkeypatch, None, tmp_path)

    assert _spa_inline_style_hashes() == ()
    assert "style-src 'self';" in _app_csp("")


def test_hashes_land_in_style_src_beside_self(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_at(monkeypatch, "<style>html{background:#f5f5f5}</style>", tmp_path)

    csp = _app_csp("")

    assert "style-src 'self' 'sha256-HZ75q+QO5LDUVc97U/plZzTbLcIxmPbaWWb0+YM2DQg='; " in csp
    # The attribute directive is a separate concern and must not absorb the hash.
    assert "style-src-attr 'unsafe-inline';" in csp


def test_served_header_carries_the_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_at(monkeypatch, "<style>html{background:#f5f5f5}</style>", tmp_path)

    app = FastAPI()
    app.middleware("http")(security_headers_middleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    resp = TestClient(app).get("/ping")

    assert (
        "'sha256-HZ75q+QO5LDUVc97U/plZzTbLcIxmPbaWWb0+YM2DQg='"
        in (resp.headers["Content-Security-Policy"])
    )


@pytest.mark.skipif(not BUNDLE.is_file(), reason="frontend release bundle not present")
def test_the_shipped_bundle_still_needs_a_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Guards the reason this exists: index.html in the reviewed bundle has a <style> block.

    If the frontend ever drops it, this fails and the machinery can go with it
    rather than lingering as unexplained policy.
    """
    with tarfile.open(BUNDLE) as archive:
        member = archive.extractfile("index.html")
        assert member is not None
        html = member.read().decode("utf-8")

    _point_at(monkeypatch, html, tmp_path)
    hashes = _spa_inline_style_hashes()

    assert len(hashes) == 1, f"bundle index.html has {len(hashes)} <style> blocks, expected 1"
    assert "Anti-flash" in html
    # Independently recomputed from the archive, so a change in extraction is caught.
    block = html.split("<style>", 1)[1].split("</style>", 1)[0]
    expected = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
    assert hashes[0] == f"'sha256-{expected}'"
    assert f"style-src 'self' 'sha256-{expected}';" in _app_csp("")
