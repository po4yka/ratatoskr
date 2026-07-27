from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cli import reconcile_source_content
from app.tasks.reconcile_source_content import SourceContentReconcileSummary


def _database() -> SimpleNamespace:
    return SimpleNamespace(dispose=AsyncMock())


@pytest.mark.asyncio
async def test_reconcile_source_content_cli_is_read_only_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _database()
    rows = [
        {
            "summary_id": 14,
            "request_id": 42,
            "has_local_source": True,
        }
    ]
    reconcile = AsyncMock()
    monkeypatch.setattr(
        reconcile_source_content, "prepare_config", MagicMock(return_value=object())
    )
    monkeypatch.setattr(
        reconcile_source_content,
        "build_runtime_database",
        MagicMock(return_value=db),
    )
    monkeypatch.setattr(
        reconcile_source_content,
        "_fetch_missing_source_rows",
        AsyncMock(return_value=rows),
    )
    monkeypatch.setattr(
        reconcile_source_content,
        "_get_missing_source_stats",
        AsyncMock(return_value=(3, 3600.0)),
    )
    monkeypatch.setattr(reconcile_source_content, "_reconcile_body", reconcile)

    result = await reconcile_source_content.run(
        reconcile_source_content.parse_args(["--limit", "10"])
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "dry_run": True,
        "missing_total": 3,
        "oldest_missing_age_seconds": 3600.0,
        "sample": [
            {
                "summary_id": 14,
                "request_id": 42,
                "has_local_source": True,
            }
        ],
    }
    reconcile.assert_not_awaited()
    db.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_source_content_cli_applies_one_bounded_batch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _database()
    cfg = object()
    summary = SourceContentReconcileSummary(
        scanned=2,
        local_repaired=2,
        reextracted=0,
        skipped=0,
        failed=0,
        missing_remaining=1,
        next_cursor=22,
    )
    reconcile = AsyncMock(return_value=summary)
    monkeypatch.setattr(reconcile_source_content, "prepare_config", MagicMock(return_value=cfg))
    monkeypatch.setattr(
        reconcile_source_content,
        "build_runtime_database",
        MagicMock(return_value=db),
    )
    monkeypatch.setattr(reconcile_source_content, "_reconcile_body", reconcile)

    result = await reconcile_source_content.run(
        reconcile_source_content.parse_args(["--apply", "--limit", "2", "--network-limit", "1"])
    )

    assert result == 0
    reconcile.assert_awaited_once_with(
        cfg,
        db,
        batch_size=2,
        network_limit=1,
    )
    assert json.loads(capsys.readouterr().out)["dry_run"] is False
    db.dispose.assert_awaited_once()
