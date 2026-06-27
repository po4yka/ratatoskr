"""Symlink-TOCTOU hardening tests for AiBackupDiskWriter.

Each test plants a symlink at a target path inside the run directory, pointing
outside it, then asserts that the write is REFUSED and the symlink target is NOT
written through.

Skipped on platforms without ``os.O_NOFOLLOW`` (e.g. Windows) because the guard
degrades gracefully there rather than raising.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from app.adapters.ai_backup.disk_writer import AiBackupDiskWriter
from app.adapters.ai_backup.errors import PathTraversalError

pytestmark = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="O_NOFOLLOW not available on this platform — symlink guard degrades safely",
)

_DATE = dt.date(2026, 6, 27)


def _writer(tmp_path) -> AiBackupDiskWriter:
    return AiBackupDiskWriter(tmp_path, "chatgpt", _DATE, "corr-sym")


# ---------------------------------------------------------------------------
# write_conversation — symlink at the target .json path
# ---------------------------------------------------------------------------


def test_conversation_symlink_refused(tmp_path) -> None:
    w = _writer(tmp_path)
    # Ensure the parent directory exists (writer creates it on first write, but
    # we need it to plant the symlink before any write occurs).
    conv_dir = w.run_dir / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)

    target_outside = tmp_path / "secret.txt"
    target_outside.write_text("original", encoding="utf-8")

    link = conv_dir / "conv1.json"
    link.symlink_to(target_outside)

    with pytest.raises(PathTraversalError):
        w.write_conversation("conv1", {"data": "evil"})

    # The target outside the run dir must be untouched.
    assert target_outside.read_text(encoding="utf-8") == "original"


# ---------------------------------------------------------------------------
# write_project — symlink at the target project.json path
# ---------------------------------------------------------------------------


def test_project_symlink_refused(tmp_path) -> None:
    w = _writer(tmp_path)
    proj_dir = w.run_dir / "projects" / "proj1"
    proj_dir.mkdir(parents=True, exist_ok=True)

    target_outside = tmp_path / "project_secret.txt"
    target_outside.write_text("safe", encoding="utf-8")

    link = proj_dir / "project.json"
    link.symlink_to(target_outside)

    with pytest.raises(PathTraversalError):
        w.write_project("proj1", {"name": "evil"})

    assert target_outside.read_text(encoding="utf-8") == "safe"


# ---------------------------------------------------------------------------
# write_file — symlink at the target file path
# ---------------------------------------------------------------------------


def test_write_file_symlink_refused(tmp_path) -> None:
    w = _writer(tmp_path)
    files_dir = w.run_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    target_outside = tmp_path / "file_secret.bin"
    target_outside.write_bytes(b"untouched")

    # The sanitized filename for file_id="file1", name="photo.png" is
    # "file1__photo.png".
    link = files_dir / "file1__photo.png"
    link.symlink_to(target_outside)

    with pytest.raises(PathTraversalError):
        w.write_file("file1", "photo.png", b"evil bytes")

    assert target_outside.read_bytes() == b"untouched"


# ---------------------------------------------------------------------------
# write_artifact — symlink at the target artifact path
# ---------------------------------------------------------------------------


def test_write_artifact_symlink_refused(tmp_path) -> None:
    w = _writer(tmp_path)
    art_dir = w.run_dir / "artifacts" / "convA"
    art_dir.mkdir(parents=True, exist_ok=True)

    target_outside = tmp_path / "artifact_secret.py"
    target_outside.write_bytes(b"# original")

    link = art_dir / "art1.py"
    link.symlink_to(target_outside)

    with pytest.raises(PathTraversalError):
        w.write_artifact("convA", "art1", "py", b"import evil")

    assert target_outside.read_bytes() == b"# original"


# ---------------------------------------------------------------------------
# idempotency read — symlink at an already-written path
# ---------------------------------------------------------------------------


def test_idempotency_read_symlink_refused(tmp_path) -> None:
    """A symlink planted between the first and second write is caught on the
    re-read during the idempotency check."""
    w = _writer(tmp_path)
    # First write succeeds (no symlink yet).
    w.write_conversation("conv_idem", {"v": 1})

    conv_path = w.run_dir / "conversations" / "conv_idem.json"
    assert conv_path.exists()

    # Replace the real file with a symlink pointing outside.
    target_outside = tmp_path / "idem_secret.json"
    target_outside.write_bytes(b"{}")
    conv_path.unlink()
    conv_path.symlink_to(target_outside)

    with pytest.raises(PathTraversalError):
        w.write_conversation("conv_idem", {"v": 1})

    # Target must not have been overwritten.
    assert target_outside.read_bytes() == b"{}"
