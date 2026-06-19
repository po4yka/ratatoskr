from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tools.scripts.build_extension_zip import EXTENSION_DIR, REQUIRED_FILES, build_zip

pytestmark = pytest.mark.no_network


def test_extension_manifest_is_manifest_v3() -> None:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert "storage" in manifest["permissions"]
    assert "activeTab" in manifest["permissions"]
    assert "<all_urls>" not in manifest.get("host_permissions", [])
    assert manifest["action"]["default_popup"] == "popup.html"
    assert manifest["icons"]["128"] == "icons/icon-128.png"
    assert manifest["action"]["default_icon"]["128"] == "icons/icon-128.png"


def test_build_extension_zip_contains_required_files(tmp_path: Path) -> None:
    output = build_zip(tmp_path / "extension.zip")

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())

    assert set(REQUIRED_FILES).issubset(names)
    assert all(not name.startswith("extension/") for name in names)


def test_extension_runtime_guards_against_reviewed_failure_modes() -> None:
    popup_js = (EXTENSION_DIR / "popup.js").read_text(encoding="utf-8")
    background_js = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")

    assert "refreshAccessToken" in popup_js
    assert "/v1/auth/refresh" in popup_js
    assert "/v1/auth/logout" in popup_js
    assert "Plain HTTP is allowed only for localhost" in popup_js
    assert "retryable: failure.retryable" in popup_js
    assert "entry.payload" in background_js
    assert "JSON.stringify(entry.payload)" in background_js
    assert "ensureRetryAlarm" in background_js
