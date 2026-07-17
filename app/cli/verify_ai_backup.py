"""Verify AI-backup evidence without contacting ChatGPT or Claude.

The command intentionally emits only aggregate counts and hashes. Provider
object identifiers and backup content stay in the operator-owned input files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

_CATEGORIES = ("conversations", "projects", "files", "artifacts")
_SERVICES = {"chatgpt", "claude"}
_SCHEMA_VERSION = "2"
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_INVENTORY_BYTES = 4 * 1024 * 1024
_MAX_JSON_PAYLOAD_BYTES = 64 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_ID_RE = re.compile(r"^[\w-]{1,128}$")
_ARTIFACT_KEY_RE = re.compile(r"^[\w-]{1,128}/[\w-]{1,128}\.[\w.]{1,16}$")


class VerificationError(ValueError):
    """A validation failure whose message is safe to print to an operator."""


def _read_regular_nofollow(path: Path, *, label: str, limit: int) -> tuple[bytes, int]:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise VerificationError(f"{label} is missing, unreadable, or unsafe") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > limit:
            raise VerificationError(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                raise VerificationError(f"{label} changed during read-back")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), metadata.st_mode
    finally:
        os.close(fd)


def _load_json_nofollow(path: Path, *, label: str, limit: int) -> tuple[dict[str, Any], bytes, int]:
    payload, mode = _read_regular_nofollow(path, label=label, limit=limit)

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise VerificationError(f"{label} contains duplicate JSON keys")
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be a JSON object")
    return value, payload, mode


def _owner_only(mode: int) -> bool:
    return stat.S_IMODE(mode) & 0o077 == 0


def _validate_run_dir(path: Path) -> Path:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        raise VerificationError("run directory is missing or unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise VerificationError("run directory must be a real directory, not a symlink")
    return absolute.resolve()


def _validate_manifest(manifest: dict[str, Any], run_dir: Path) -> dict[str, dict[str, str]]:
    if manifest.get("schema_version") != _SCHEMA_VERSION:
        raise VerificationError("manifest schema version is unsupported")
    service = manifest.get("service")
    if service not in _SERVICES:
        raise VerificationError("manifest service is unsupported")
    run_date = manifest.get("run_date")
    if not isinstance(run_date, str):
        raise VerificationError("manifest run date is invalid")
    try:
        dt.date.fromisoformat(run_date)
    except ValueError as exc:
        raise VerificationError("manifest run date is invalid") from exc
    if run_dir.name != run_date or run_dir.parent.name != service:
        raise VerificationError("manifest identity does not match its run directory")
    if not isinstance(manifest.get("incremental"), bool):
        raise VerificationError("manifest incremental flag is invalid")

    counts = manifest.get("counts")
    metadata = manifest.get("run_metadata")
    if not isinstance(counts, dict) or not isinstance(metadata, dict):
        raise VerificationError("manifest aggregate metadata is invalid")
    collected = metadata.get("collected_counts")
    if not isinstance(collected, dict):
        raise VerificationError("manifest collected counts are invalid")
    try:
        started_at = dt.datetime.fromisoformat(metadata["started_at"])
        finished_at = dt.datetime.fromisoformat(metadata["finished_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VerificationError("manifest timestamps are invalid") from exc
    if started_at.tzinfo is None or finished_at.tzinfo is None or started_at > finished_at:
        raise VerificationError("manifest timestamps are invalid")
    for field in ("requests_made", "skipped_incremental"):
        value = metadata.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VerificationError("manifest run counters are invalid")

    entries_by_category: dict[str, dict[str, str]] = {}
    for category in _CATEGORIES:
        entries = manifest.get(category)
        if not isinstance(entries, dict):
            raise VerificationError(f"manifest {category} inventory is invalid")
        typed: dict[str, str] = {}
        for key, digest in entries.items():
            canonical_key = (
                bool(_ARTIFACT_KEY_RE.fullmatch(key))
                if category == "artifacts" and isinstance(key, str)
                else bool(_ID_RE.fullmatch(key))
                if isinstance(key, str)
                else False
            )
            if (
                not isinstance(key, str)
                or not key
                or not canonical_key
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise VerificationError(f"manifest {category} inventory is invalid")
            typed[key] = digest
        count = counts.get(category)
        collected_count = collected.get(category)
        if isinstance(count, bool) or not isinstance(count, int) or count != len(typed):
            raise VerificationError(f"manifest {category} count is inconsistent")
        if (
            isinstance(collected_count, bool)
            or not isinstance(collected_count, int)
            or collected_count < 0
        ):
            raise VerificationError(f"manifest {category} collected count is invalid")
        entries_by_category[category] = typed
    return entries_by_category


def _validate_expected_inventory(
    expected: dict[str, Any], manifest: dict[str, Any], entries: dict[str, dict[str, str]]
) -> None:
    if (
        expected.get("service") != manifest["service"]
        or expected.get("run_date") != manifest["run_date"]
    ):
        raise VerificationError("expected inventory identifies a different backup run")
    for category in _CATEGORIES:
        values = expected.get(category)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))
        ):
            raise VerificationError(f"expected {category} inventory is invalid")
        if set(values) != set(entries[category]):
            raise VerificationError(
                f"expected {category} inventory does not match ({len(values)} expected, "
                f"{len(entries[category])} observed)"
            )


def _assert_safe_relative_file(run_dir: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise VerificationError("manifest contains an unsafe file mapping")
    cursor = run_dir
    for part in relative.parts[:-1]:
        cursor /= part
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise VerificationError("manifest references missing backup data") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise VerificationError("backup data contains an unsafe path component")
    return run_dir / relative


def _verify_payload(path: Path, category: str, expected_digest: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise VerificationError("backup payload is missing, unreadable, or unsafe") from exc
    try:
        metadata = os.fstat(fd)
        json_payload = category in {"conversations", "projects"}
        size_limit = _MAX_JSON_PAYLOAD_BYTES if json_payload else _MAX_PAYLOAD_BYTES
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > size_limit
        ):
            raise VerificationError(f"backup {category} payload is not a bounded regular file")
        if not _owner_only(metadata.st_mode):
            raise VerificationError(f"backup {category} payload is not owner-only")

        digest = hashlib.sha256()
        restored_chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                raise VerificationError(f"backup {category} payload changed during read-back")
            digest.update(chunk)
            if json_payload:
                restored_chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)

    if digest.hexdigest() != expected_digest:
        raise VerificationError(f"backup {category} hash read-back failed")
    if json_payload:
        try:
            restored = json.loads(b"".join(restored_chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError(f"backup {category} JSON read-back failed") from exc
        if not isinstance(restored, dict):
            raise VerificationError(f"backup {category} JSON read-back failed")


def _payload_path(category: str, identifier: str, *, file_names: tuple[str, ...]) -> Path:
    if category == "conversations":
        return Path(category, f"{identifier}.json")
    if category == "projects":
        return Path(category, identifier, "project.json")
    if category == "artifacts":
        candidate = Path(category, identifier)
        if len(candidate.parts) != 3:
            raise VerificationError("manifest artifacts inventory is invalid")
        return candidate
    prefix = f"{identifier}__"
    matches = tuple(name for name in file_names if name.startswith(prefix))
    if len(matches) != 1:
        raise VerificationError("manifest files inventory cannot be mapped uniquely")
    return Path("files", matches[0])


def _walk_regular_files(run_dir: Path) -> set[Path]:
    observed: set[Path] = set()
    for directory, dir_names, file_names in os.walk(run_dir, followlinks=False):
        current = Path(directory)
        for name in dir_names:
            try:
                metadata = (current / name).lstat()
            except OSError as exc:
                raise VerificationError("backup tree changed during inspection") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise VerificationError("backup tree contains an unsafe directory")
        for name in file_names:
            candidate = current / name
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                raise VerificationError("backup tree changed during inspection") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise VerificationError("backup tree contains an unsafe file")
            observed.add(candidate.relative_to(run_dir))
    return observed


def verify_backup(run_dir: Path, expected_inventory: Path) -> dict[str, Any]:
    """Verify one manifest and return a content-free evidence summary."""
    root = _validate_run_dir(run_dir)
    manifest, manifest_bytes, manifest_mode = _load_json_nofollow(
        root / "manifest.json", label="manifest", limit=_MAX_MANIFEST_BYTES
    )
    expected, _, expected_mode = _load_json_nofollow(
        expected_inventory.absolute(), label="expected inventory", limit=_MAX_INVENTORY_BYTES
    )
    if not _owner_only(manifest_mode) or not _owner_only(expected_mode):
        raise VerificationError("manifest and expected inventory must be owner-only files")

    entries = _validate_manifest(manifest, root)
    _validate_expected_inventory(expected, manifest, entries)

    try:
        file_names = tuple(entry.name for entry in (root / "files").iterdir())
    except FileNotFoundError:
        file_names = ()
    except OSError as exc:
        raise VerificationError("backup files directory is unsafe or unreadable") from exc

    mapped = {Path("manifest.json")}
    for category in _CATEGORIES:
        for identifier, expected_digest in entries[category].items():
            relative = _payload_path(category, identifier, file_names=file_names)
            path = _assert_safe_relative_file(root, relative)
            _verify_payload(path, category, expected_digest)
            mapped.add(relative)
    if _walk_regular_files(root) != mapped:
        raise VerificationError("backup tree contains files not covered by the manifest")

    return {
        "status": "offline_integrity_passed",
        "schema_version": _SCHEMA_VERSION,
        "service": manifest["service"],
        "run_date": manifest["run_date"],
        "incremental": manifest["incremental"],
        "counts": {category: len(entries[category]) for category in _CATEGORIES},
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "hash_readback": "passed",
        "file_modes": "owner-only",
        "provider_compatibility": "unverified",
        "project_knowledge": "unverified",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify an AI-backup manifest and payloads without provider access."
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--expected-inventory", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = verify_backup(args.run_dir, args.expected_inventory)
    except VerificationError as exc:
        print(f"AI backup evidence verification failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "AI backup evidence verification failed: unexpected unsafe or unreadable input",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
