from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from app.adapters.ai_backup.disk_writer import AiBackupDiskWriter
from app.cli.verify_ai_backup import VerificationError, main, verify_backup


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    writer = AiBackupDiskWriter(
        tmp_path / "backups",
        "chatgpt",
        dt.date(2026, 7, 17),
        "secret-correlation-id",
    )
    writer.write_conversation("conv-secret", {"id": "conv-secret", "text": "private"})
    writer.write_project("project-secret", {"id": "project-secret"})
    writer.write_file("file-secret", "private.txt", b"private attachment")
    writer.finalize_manifest(
        {"conversations": 1, "projects": 1, "files": 1, "artifacts": 0},
        requests_made=4,
        skipped_incremental=0,
        incremental=False,
    )
    inventory = tmp_path / "expected.json"
    inventory.write_text(
        json.dumps(
            {
                "service": "chatgpt",
                "run_date": "2026-07-17",
                "conversations": ["conv-secret"],
                "projects": ["project-secret"],
                "files": ["file-secret"],
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    inventory.chmod(0o600)
    return writer.run_dir, inventory


def test_verify_backup_returns_sanitized_aggregate_evidence(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)

    evidence = verify_backup(run_dir, inventory)

    rendered = json.dumps(evidence)
    assert evidence["status"] == "offline_integrity_passed"
    assert evidence["provider_compatibility"] == "unverified"
    assert evidence["project_knowledge"] == "unverified"
    assert evidence["counts"] == {
        "conversations": 1,
        "projects": 1,
        "files": 1,
        "artifacts": 0,
    }
    assert "conv-secret" not in rendered
    assert "private" not in rendered
    assert "secret-correlation-id" not in rendered


def test_main_reports_inventory_mismatch_without_identifier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, inventory = _fixture(tmp_path)
    data = json.loads(inventory.read_text(encoding="utf-8"))
    data["conversations"] = ["different-secret-id"]
    inventory.write_text(json.dumps(data), encoding="utf-8")

    assert main(["--run-dir", str(run_dir), "--expected-inventory", str(inventory)]) == 1

    output = capsys.readouterr()
    assert "inventory does not match (1 expected, 1 observed)" in output.err
    assert "conv-secret" not in output.err
    assert "different-secret-id" not in output.err


def test_verify_backup_rejects_payload_hash_mismatch(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    (run_dir / "conversations" / "conv-secret.json").write_text("tampered", encoding="utf-8")

    with pytest.raises(VerificationError, match="hash read-back failed"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_inconsistent_manifest_count(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["conversations"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(VerificationError, match="count is inconsistent"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_duplicate_manifest_keys(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    manifest_path = run_dir / "manifest.json"
    raw = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        raw.replace('{\n  "schema_version"', '{\n  "schema_version": "2",\n  "schema_version"'),
        encoding="utf-8",
    )

    with pytest.raises(VerificationError, match="duplicate JSON keys"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_reversed_timestamps(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_metadata"]["started_at"] = "2026-07-17T12:00:00+00:00"
    manifest["run_metadata"]["finished_at"] = "2026-07-17T11:00:00+00:00"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(VerificationError, match="timestamps are invalid"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_noncanonical_manifest_identifier(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = manifest["conversations"].pop("conv-secret")
    manifest["conversations"]["../secret"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(VerificationError, match="inventory is invalid"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_symlinked_payload(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    payload = run_dir / "conversations" / "conv-secret.json"
    outside = tmp_path / "outside.json"
    outside.write_text(payload.read_text(encoding="utf-8"), encoding="utf-8")
    payload.unlink()
    payload.symlink_to(outside)

    with pytest.raises(VerificationError, match=r"unsafe|unreadable"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    link = tmp_path / "linked-run"
    link.symlink_to(run_dir, target_is_directory=True)

    with pytest.raises(VerificationError, match="not a symlink"):
        verify_backup(link, inventory)


def test_verify_backup_rejects_extra_unhashed_file(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    extra = run_dir / "conversations" / "untracked.json"
    extra.write_text("{}", encoding="utf-8")
    extra.chmod(0o600)

    with pytest.raises(VerificationError, match="not covered"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_world_readable_payload(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    payload = run_dir / "conversations" / "conv-secret.json"
    payload.chmod(0o644)

    with pytest.raises(VerificationError, match="not owner-only"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_world_readable_directory(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    (run_dir / "conversations").chmod(0o755)

    with pytest.raises(VerificationError, match="non-owner-only directory"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_world_readable_expected_inventory(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    inventory.chmod(0o644)

    with pytest.raises(VerificationError, match="owner-only"):
        verify_backup(run_dir, inventory)


def test_verify_backup_rejects_hardlinked_payload(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    payload = run_dir / "conversations" / "conv-secret.json"
    os.link(payload, tmp_path / "second-link.json")

    with pytest.raises(VerificationError, match="bounded regular file"):
        verify_backup(run_dir, inventory)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires O_NOFOLLOW")
def test_verify_backup_rejects_symlinked_manifest(tmp_path: Path) -> None:
    run_dir, inventory = _fixture(tmp_path)
    manifest = run_dir / "manifest.json"
    outside = tmp_path / "manifest.json"
    outside.write_bytes(manifest.read_bytes())
    outside.chmod(0o600)
    manifest.unlink()
    manifest.symlink_to(outside)

    with pytest.raises(VerificationError, match="unsafe"):
        verify_backup(run_dir, inventory)
