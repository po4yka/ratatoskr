"""Tests for the AI account backup on-disk writer."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os

import pytest

from app.adapters.ai_backup.disk_writer import (
    AiBackupDiskWriter,
    _safe_child,
    _sanitize_id,
    _write_nofollow,
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


def test_load_saved_conversation_resume(tmp_path) -> None:
    w = _writer(tmp_path)
    # Nothing saved yet.
    assert w.load_saved_conversation("conv1") is None
    # After a write it is readable back and registered in the manifest.
    w.write_conversation("conv1", {"a": 1})
    fresh = _writer(tmp_path)  # a new run object for the same run dir/date
    loaded = fresh.load_saved_conversation("conv1")
    assert loaded == {"a": 1}
    # Resumed conversation is registered so a partial manifest stays complete.
    assert fresh.partial_counts()["conversations"] == 1
    # A non-dict / corrupt blob is treated as absent (caller re-fetches).
    (w.run_dir / "conversations" / "conv1.json").write_text("not json")
    assert _writer(tmp_path).load_saved_conversation("conv1") is None


def test_write_file_replaces_changed_existing_bytes(tmp_path) -> None:
    w = _writer(tmp_path)
    w.write_file("file1", "photo.png", b"abc")
    path = next((w.run_dir / "files").iterdir())
    w.write_file("file1", "photo.png", b"DIFFERENT")
    assert path.read_bytes() == b"DIFFERENT"
    manifest = w.finalize_manifest(
        {"files": 1}, requests_made=2, skipped_incremental=0, incremental=False
    )
    assert manifest["files"]["file1"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_write_nofollow_retries_short_writes(tmp_path, monkeypatch) -> None:
    target = tmp_path / "blob"
    real_write = os.write

    def _short_write(fd: int, data: bytes | memoryview) -> int:
        chunk = data[: max(1, len(data) // 2)]
        return real_write(fd, chunk)

    monkeypatch.setattr(os, "write", _short_write)
    _write_nofollow(target, b"complete payload")
    assert target.read_bytes() == b"complete payload"


def test_failed_replacement_preserves_previous_file(tmp_path, monkeypatch) -> None:
    w = _writer(tmp_path)
    w.write_file("file1", "photo.png", b"original")
    path = next((w.run_dir / "files").iterdir())
    real_write = os.write
    calls = 0

    def _fail_mid_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:1])
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", _fail_mid_write)
    with pytest.raises(OSError, match="disk full"):
        w.write_file("file1", "photo.png", b"replacement")
    assert path.read_bytes() == b"original"


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
