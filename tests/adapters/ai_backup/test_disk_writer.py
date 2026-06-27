"""Tests for the AI account backup on-disk writer."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app.adapters.ai_backup.disk_writer import (
    AiBackupDiskWriter,
    _safe_child,
    _sanitize_id,
)
from app.adapters.ai_backup.errors import PathTraversalError

_DATE = dt.date(2026, 6, 27)


def _writer(tmp_path) -> AiBackupDiskWriter:
    return AiBackupDiskWriter(tmp_path, "chatgpt", _DATE, "corr-1")


def test_sanitize_id_neutralizes_traversal() -> None:
    assert _sanitize_id("../../../etc/passwd") == "_________etc_passwd"
    assert _sanitize_id("\x00") == "_"
    assert len(_sanitize_id("g-" + "a" * 300)) == 128
    assert _sanitize_id("") == "_"


def test_safe_child_blocks_escape(tmp_path) -> None:
    root = tmp_path.resolve()
    with pytest.raises(PathTraversalError):
        _safe_child(root, "../sibling")
    with pytest.raises(PathTraversalError):
        _safe_child(root)  # equals root
    assert _safe_child(root, "a", "b") == root / "a" / "b"


def test_write_conversation_idempotent(tmp_path) -> None:
    w = _writer(tmp_path)
    w.write_conversation("conv1", {"a": 1})
    path = w.run_dir / "conversations" / "conv1.json"
    assert path.exists()
    mtime = path.stat().st_mtime_ns
    # Same payload again: file is not rewritten.
    w.write_conversation("conv1", {"a": 1})
    assert path.stat().st_mtime_ns == mtime
    # Different payload: file is rewritten.
    w.write_conversation("conv1", {"a": 2})
    assert json.loads(path.read_text())["a"] == 2


def test_write_file_idempotent_skips_existing(tmp_path) -> None:
    w = _writer(tmp_path)
    w.write_file("file1", "photo.png", b"abc")
    path = next((w.run_dir / "files").iterdir())
    mtime = path.stat().st_mtime_ns
    w.write_file("file1", "photo.png", b"DIFFERENT")  # same id -> not rewritten
    assert path.stat().st_mtime_ns == mtime
    assert path.read_bytes() == b"abc"


def test_write_artifact_separates_by_conv_and_id(tmp_path) -> None:
    w = _writer(tmp_path)
    w.write_artifact("convA", "art1", "py", b"print(1)")
    w.write_artifact("convB", "art1", "py", b"print(2)")
    assert (w.run_dir / "artifacts" / "convA" / "art1.py").read_bytes() == b"print(1)"
    assert (w.run_dir / "artifacts" / "convB" / "art1.py").read_bytes() == b"print(2)"


@pytest.mark.parametrize("conv_id", ["../escape", "/absolute", "a\x00b", "a/b/c"])
def test_remote_ids_cannot_escape_run_dir(tmp_path, conv_id: str) -> None:
    w = _writer(tmp_path)
    # Sanitized ids never escape; the file lands inside the run dir.
    w.write_conversation(conv_id, {"x": 1})
    for path in (w.run_dir / "conversations").iterdir():
        assert path.resolve().is_relative_to(w.run_dir.resolve())


def test_finalize_manifest_atomic_and_complete(tmp_path) -> None:
    w = _writer(tmp_path)
    w.write_conversation("c1", {"x": 1})
    manifest = w.finalize_manifest(
        {"conversations": 1, "projects": 0, "files": 0, "artifacts": 0},
        requests_made=4,
        skipped_incremental=2,
        incremental=True,
    )
    on_disk = json.loads((w.run_dir / "manifest.json").read_text())
    assert on_disk == manifest
    assert not (w.run_dir / ".manifest.json.tmp").exists()
    assert manifest["schema_version"] == "1"
    assert manifest["counts"]["conversations"] == 1
    assert manifest["run_metadata"]["requests_made"] == 4
    assert manifest["run_metadata"]["skipped_incremental"] == 2
    assert "c1" in manifest["conversations"]
