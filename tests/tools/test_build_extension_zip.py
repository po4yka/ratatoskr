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
    assert manifest["action"]["default_popup"] == "popup.html"


def test_build_extension_zip_contains_required_files(tmp_path: Path) -> None:
    output = build_zip(tmp_path / "extension.zip")

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())

    assert set(REQUIRED_FILES).issubset(names)
    assert all(not name.startswith("extension/") for name in names)
