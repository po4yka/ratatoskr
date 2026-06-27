"""On-disk writer for AI account backups.

All filesystem writes for a single backup run go through this class. No network,
no DB. Every remote-supplied id/filename is sanitized and every resolved path is
proven to live strictly inside the run directory before a write happens, so a
hostile or malformed response can never escape ``AI_BACKUP_DATA_PATH``.

Layout::

    <data_root>/<service>/<YYYY-MM-DD>/
      conversations/<conv_id>.json
      projects/<project_id>/project.json
      files/<file_id>__<name>
      artifacts/<conv_id>/<artifact_id>.<ext>
      manifest.json
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

from app.adapters.ai_backup.errors import PathTraversalError

_SAFE_ID_RE = re.compile(r"[^\w\-]")
# Filenames keep dots (extensions) and hyphens; the directory component is
# stripped first and every path is containment-checked, so dots are safe here.
_SAFE_NAME_RE = re.compile(r"[^\w.\-]")
_SAFE_EXT_RE = re.compile(r"[^\w.]")
_MANIFEST_SCHEMA_VERSION = "1"


def _sanitize_id(raw: str) -> str:
    """Replace every unsafe char with ``_`` and cap at 128 chars. Never empty."""
    return _SAFE_ID_RE.sub("_", raw)[:128] or "_"


def _sanitize_filename(name: str) -> str:
    """Strip any directory component, sanitize, cap at 200 chars. Never empty."""
    return _SAFE_NAME_RE.sub("_", Path(name).name)[:200] or "_"


def _sanitize_ext(ext: str) -> str:
    return _SAFE_EXT_RE.sub("", ext).lstrip(".")[:16] or "bin"


def _safe_child(root: Path, *parts: str) -> Path:
    """Resolve ``root/parts`` and raise if it escapes ``root`` (or equals it)."""
    candidate = root.joinpath(*parts).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise PathTraversalError(
            f"Resolved path {candidate!r} is outside or equal to data root {root!r}"
        )
    return candidate


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AiBackupDiskWriter:
    """Path-safe, idempotent-by-id writer for one backup run."""

    def __init__(
        self,
        data_root: Path,
        service: str,
        run_date: dt.date,
        correlation_id: str,
        *,
        started_at: dt.datetime | None = None,
    ) -> None:
        self._data_root = Path(data_root).resolve()
        self._service = _sanitize_id(service)
        self._run_date = run_date
        self._correlation_id = correlation_id
        self._started_at = started_at or dt.datetime.now(tz=dt.UTC)
        self._run_dir = _safe_child(self._data_root, self._service, run_date.isoformat())
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._manifest: dict[str, dict[str, str]] = {
            "conversations": {},
            "projects": {},
            "files": {},
            "artifacts": {},
        }

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def _write_idempotent(self, path: Path, data: bytes) -> str:
        """Write ``data`` to ``path`` unless an identical blob is already there.

        Returns the SHA-256 hex of ``data`` regardless. Creates parent dirs.
        """
        sha = _sha256_hex(data)
        if path.exists() and _sha256_hex(path.read_bytes()) == sha:
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return sha

    def write_conversation(self, conv_id: str, payload: dict) -> None:
        sid = _sanitize_id(conv_id)
        path = _safe_child(self._run_dir, "conversations", f"{sid}.json")
        blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._manifest["conversations"][sid] = self._write_idempotent(path, blob)

    def write_project(self, project_id: str, project_data: dict) -> None:
        sid = _sanitize_id(project_id)
        path = _safe_child(self._run_dir, "projects", sid, "project.json")
        blob = json.dumps(project_data, ensure_ascii=False, indent=2).encode("utf-8")
        self._manifest["projects"][sid] = self._write_idempotent(path, blob)

    def write_file(self, file_id: str, original_name: str, data: bytes) -> None:
        sid = _sanitize_id(file_id)
        name = _sanitize_filename(original_name)
        path = _safe_child(self._run_dir, "files", f"{sid}__{name}")
        # Same file_id always means identical bytes by construction: skip if present.
        if path.exists():
            self._manifest["files"][sid] = _sha256_hex(data)
            return
        self._manifest["files"][sid] = self._write_idempotent(path, data)

    def write_artifact(self, conv_id: str, artifact_id: str, ext: str, data: bytes) -> None:
        cid = _sanitize_id(conv_id)
        aid = _sanitize_id(artifact_id)
        safe_ext = _sanitize_ext(ext)
        path = _safe_child(self._run_dir, "artifacts", cid, f"{aid}.{safe_ext}")
        key = f"{cid}/{aid}.{safe_ext}"
        if path.exists():
            self._manifest["artifacts"][key] = _sha256_hex(data)
            return
        self._manifest["artifacts"][key] = self._write_idempotent(path, data)

    def finalize_manifest(
        self,
        counts: dict[str, int],
        *,
        requests_made: int,
        skipped_incremental: int,
        incremental: bool,
    ) -> dict:
        """Write ``manifest.json`` atomically (tmp + os.replace) and return it."""
        manifest = {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "service": self._service,
            "run_date": self._run_date.isoformat(),
            "correlation_id": self._correlation_id,
            "incremental": incremental,
            "run_metadata": {
                "started_at": self._started_at.isoformat(),
                "finished_at": dt.datetime.now(tz=dt.UTC).isoformat(),
                "requests_made": requests_made,
                "skipped_incremental": skipped_incremental,
            },
            "counts": {
                "conversations": int(counts.get("conversations", 0)),
                "projects": int(counts.get("projects", 0)),
                "files": int(counts.get("files", 0)),
                "artifacts": int(counts.get("artifacts", 0)),
            },
            "conversations": self._manifest["conversations"],
            "projects": self._manifest["projects"],
            "files": self._manifest["files"],
            "artifacts": self._manifest["artifacts"],
        }
        blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        tmp = self._run_dir / ".manifest.json.tmp"
        tmp.write_bytes(blob)
        os.replace(tmp, self._run_dir / "manifest.json")
        return manifest


__all__ = ["AiBackupDiskWriter"]
