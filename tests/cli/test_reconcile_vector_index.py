from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cli import reconcile_vector_index


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        embedding=SimpleNamespace(
            provider="gemini",
            gemini_model="test-model",
            embedding_dim=3,
        ),
        vector_store=SimpleNamespace(
            url="http://qdrant.test",
            api_key=None,
            environment="test",
            user_scope="owner",
            collection_version="v1",
            required=False,
            connection_timeout=1.0,
        ),
        vector_reconcile=SimpleNamespace(batch_size=7),
    )


@pytest.mark.asyncio
async def test_reconcile_repair_invokes_summary_and_repository_backfills(monkeypatch) -> None:
    fake_db = MagicMock()
    fake_db.dispose = AsyncMock()
    fake_report = MagicMock()
    fake_report.to_diagnostics.return_value = {"status": "degraded"}

    class FakeReconciler:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def inspect(self):
            return fake_report

    class FakeVectorStore:
        available = True

    summary_backfill = AsyncMock()
    repository_backfill = AsyncMock(return_value={"processed": 1})

    monkeypatch.setattr(reconcile_vector_index, "load_config", lambda **_kwargs: _cfg())
    monkeypatch.setattr(reconcile_vector_index, "DatabaseConfig", lambda dsn=None: MagicMock())
    monkeypatch.setattr(reconcile_vector_index, "Database", lambda config: fake_db)
    monkeypatch.setattr(
        "app.infrastructure.vector.qdrant_store.QdrantVectorStore",
        lambda **_kwargs: FakeVectorStore(),
    )
    monkeypatch.setattr(reconcile_vector_index, "VectorIndexReconciler", FakeReconciler)
    monkeypatch.setattr(reconcile_vector_index, "backfill_vector_store", summary_backfill)
    monkeypatch.setattr(
        reconcile_vector_index,
        "backfill_repository_embeddings",
        repository_backfill,
    )

    result = await reconcile_vector_index.reconcile_vector_index(
        database_dsn="postgresql://test",
        repair=True,
        dry_run=True,
        limit=5,
    )

    assert result == {"report": {"status": "degraded"}, "repository_repair": {"processed": 1}}
    summary_backfill.assert_awaited_once()
    repository_backfill.assert_awaited_once_with(
        database_dsn="postgresql://test",
        dry_run=True,
        batch_size=7,
        model_version_target="1.0",
    )
    fake_db.dispose.assert_awaited_once()


def test_reconcile_vector_index_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr(reconcile_vector_index.sys, "argv", ["reconcile_vector_index.py", "--help"])

    assert reconcile_vector_index.main() == 0
    assert "--repair" in capsys.readouterr().out
