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

TOCTOU hardening
----------------
``_safe_child`` resolves and containment-checks the path at planning time, but a
race could plant a symlink at the final component between that check and the
actual open.  ``_write_nofollow`` and ``_read_nofollow`` close this window by
opening the file with ``O_NOFOLLOW`` (where available), which causes the kernel
to raise ``OSError`` (``ELOOP``) if the final path component is a symlink.  On
platforms that lack ``O_NOFOLLOW`` (non-POSIX) the flag degrades to 0, so writes
are still atomic — they just lack the symlink guard; the containment check still
runs.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from app.adapters.ai_backup.errors import PathTraversalError

_SAFE_ID_RE = re.compile(r"[^\w\-]")
# Filenames keep dots (extensions) and hyphens; the directory component is
# stripped first and every path is containment-checked, so dots are safe here.
_SAFE_NAME_RE = re.compile(r"[^\w.\-]")
_SAFE_EXT_RE = re.compile(r"[^\w.]")
_MANIFEST_SCHEMA_VERSION = "2"

# O_NOFOLLOW is POSIX but absent on Windows/some exotic platforms; degrade safely.
_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)


def _enforce_private_file_mode(fd: int) -> None:
    os.fchmod(fd, 0o600)
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise PermissionError(
            errno.EACCES,
            "Filesystem did not enforce owner-only AI backup file permissions",
        )


def _enforce_private_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    except OSError as exc:
        raise PathTraversalError("Backup file is unsafe or inaccessible") from exc
    try:
        _enforce_private_file_mode(fd)
    finally:
        os.close(fd)


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


def _write_nofollow(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Write *data* to *path* without following a symlink at the final component.

    Opens with ``O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW`` (0o600 mode).
    If the final path component is a symlink ``os.open`` raises ``OSError``
    (``ELOOP`` on Linux/macOS).  That exception is caught by callers and re-raised
    as ``PathTraversalError``.

    On platforms without ``O_NOFOLLOW`` the flag is 0, so the write proceeds
    normally; the containment check in ``_safe_child`` still protects against
    resolved-path escapes.
    """
    flags = os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        _enforce_private_file_mode(fd)
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("write returned no progress")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_nofollow(path: Path, data: bytes) -> None:
    """Write through a private sibling and atomically replace ``path``."""
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        _write_nofollow(tmp, data, exclusive=True)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_nofollow(path: Path) -> bytes | None:
    """Read *path* without following a symlink at the final component.

    Returns ``None`` if the path does not exist.  Raises ``OSError`` (``ELOOP``)
    if the final component is a symlink — callers treat that as a security event.
    """
    flags = os.O_RDONLY | _O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _ensure_private_directory_tree(root: Path, target: Path) -> None:
    """Create/chmod every directory from ``root`` through ``target`` to 0700."""
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise PathTraversalError("Backup directory is outside the data root") from exc

    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        try:
            current.mkdir(mode=0o700, parents=current == root, exist_ok=True)
            fd = os.open(current, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        except OSError as exc:
            raise PathTraversalError("Backup directory is unsafe or inaccessible") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISDIR(metadata.st_mode):
                raise PathTraversalError("Backup directory path is not a directory")
            os.fchmod(fd, 0o700)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise PermissionError(
                    errno.EACCES,
                    "Filesystem did not enforce owner-only AI backup directory permissions",
                )
        finally:
            os.close(fd)


def _harden_existing_tree(root: Path) -> None:
    """Upgrade legacy entries below ``root`` and fail closed on unsafe files."""
    for directory, names, file_names in os.walk(root, followlinks=False):
        for name in names:
            candidate = Path(directory) / name
            _ensure_private_directory_tree(candidate, candidate)
        for name in file_names:
            _enforce_private_file(Path(directory) / name)


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
        min_free_bytes: int = 0,
    ) -> None:
        self._data_root = Path(data_root).resolve()
        self._service = _sanitize_id(service)
        self._run_date = run_date
        self._correlation_id = correlation_id
        self._started_at = started_at or dt.datetime.now(tz=dt.UTC)
        self._min_free_bytes = min_free_bytes
        self._ensure_free_space(0)
        _ensure_private_directory_tree(self._data_root, self._data_root)
        self._run_dir = _safe_child(self._data_root, self._service, run_date.isoformat())
        _ensure_private_directory_tree(self._data_root, self._run_dir)
        _harden_existing_tree(self._run_dir)
        self._manifest: dict[str, dict[str, str]] = {
            "conversations": {},
            "projects": {},
            "files": {},
            "artifacts": {},
        }
        self._merge_existing_manifest()

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def _ensure_free_space(self, growth_bytes: int) -> None:
        probe = self._data_root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        required = self._min_free_bytes + max(0, growth_bytes)
        if free < required:
            raise OSError(
                errno.ENOSPC,
                f"AI backup requires {required} free bytes but only {free} remain",
            )

    def _merge_existing_manifest(self) -> None:
        """Carry forward hashes already committed to today's run directory."""
        path = self._run_dir / "manifest.json"
        try:
            data = _read_nofollow(path)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PathTraversalError("Symlink detected at existing manifest") from exc
            raise
        if data is None:
            return
        try:
            existing = json.loads(data)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Existing AI backup manifest is invalid JSON") from exc
        if not isinstance(existing, dict):
            raise ValueError("Existing AI backup manifest must be an object")
        if existing.get("service") != self._service or existing.get("run_date") != str(
            self._run_date
        ):
            raise ValueError("Existing AI backup manifest belongs to a different run directory")
        for category in self._manifest:
            entries = existing.get(category, {})
            if not isinstance(entries, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in entries.items()
            ):
                raise ValueError(f"Existing AI backup manifest has invalid {category}")
            self._manifest[category].update(entries)

    def _write_idempotent(self, path: Path, data: bytes) -> str:
        """Write ``data`` to ``path`` unless an identical blob is already there.

        Returns the SHA-256 hex of ``data`` regardless. Creates parent dirs.

        Both the read (idempotency check) and the write use ``O_NOFOLLOW`` so a
        symlink planted between ``_safe_child`` and this call cannot redirect I/O
        outside the run directory.
        """
        sha = _sha256_hex(data)
        try:
            existing = _read_nofollow(path)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PathTraversalError(
                    f"Symlink detected at {path!r} during idempotency read — refusing"
                ) from exc
            raise
        if existing is not None and _sha256_hex(existing) == sha:
            _enforce_private_file(path)
            return sha
        self._ensure_free_space(len(data) - len(existing or b""))
        _ensure_private_directory_tree(self._run_dir, path.parent)
        try:
            _atomic_write_nofollow(path, data)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PathTraversalError(
                    f"Symlink detected at {path!r} — write refused to prevent escape"
                ) from exc
            raise
        return sha

    def write_conversation(self, conv_id: str, payload: dict) -> None:
        sid = _sanitize_id(conv_id)
        path = _safe_child(self._run_dir, "conversations", f"{sid}.json")
        blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._manifest["conversations"][sid] = self._write_idempotent(path, blob)

    def load_saved_conversation(self, conv_id: str) -> dict | None:
        """Return a conversation already saved in this run dir, or ``None``.

        Lets a re-run after an interrupted sweep (e.g. a rate-limit 429) skip the
        network fetch for conversations already on disk for this run date, so the
        backup converges across retries instead of re-fetching everything and
        re-tripping the limit. The on-disk blob's hash is registered in the
        manifest so a resumed run's manifest stays complete. A symlink or
        unparseable file is treated as absent so the caller re-fetches and
        overwrites it through the path-safe write path.
        """
        sid = _sanitize_id(conv_id)
        path = _safe_child(self._run_dir, "conversations", f"{sid}.json")
        try:
            data = _read_nofollow(path)
        except OSError:
            return None
        if data is None:
            return None
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        self._manifest["conversations"][sid] = _sha256_hex(data)
        return parsed

    def write_project(self, project_id: str, project_data: dict) -> None:
        sid = _sanitize_id(project_id)
        path = _safe_child(self._run_dir, "projects", sid, "project.json")
        blob = json.dumps(project_data, ensure_ascii=False, indent=2).encode("utf-8")
        self._manifest["projects"][sid] = self._write_idempotent(path, blob)

    def write_file(self, file_id: str, original_name: str, data: bytes) -> None:
        sid = _sanitize_id(file_id)
        name = _sanitize_filename(original_name)
        path = _safe_child(self._run_dir, "files", f"{sid}__{name}")
        self._manifest["files"][sid] = self._write_idempotent(path, data)

    def write_artifact(self, conv_id: str, artifact_id: str, ext: str, data: bytes) -> None:
        cid = _sanitize_id(conv_id)
        aid = _sanitize_id(artifact_id)
        safe_ext = _sanitize_ext(ext)
        path = _safe_child(self._run_dir, "artifacts", cid, f"{aid}.{safe_ext}")
        key = f"{cid}/{aid}.{safe_ext}"
        self._manifest["artifacts"][key] = self._write_idempotent(path, data)

    def partial_counts(self) -> dict[str, int]:
        """Counts of what has actually been written so far this run.

        Used to finalize a partial manifest after an interrupted sweep so the
        recorded counts match what is on disk even when the run did not finish.
        """
        return {category: len(entries) for category, entries in self._manifest.items()}

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
                "collected_counts": {
                    category: int(counts.get(category, 0)) for category in self._manifest
                },
            },
            "counts": {category: len(entries) for category, entries in self._manifest.items()},
            "conversations": self._manifest["conversations"],
            "projects": self._manifest["projects"],
            "files": self._manifest["files"],
            "artifacts": self._manifest["artifacts"],
        }
        blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            _atomic_write_nofollow(self._run_dir / "manifest.json", blob)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PathTraversalError("Symlink detected during manifest write") from exc
            raise
        return manifest


__all__ = ["AiBackupDiskWriter"]
