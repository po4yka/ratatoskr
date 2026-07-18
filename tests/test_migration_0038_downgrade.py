"""Migration 0038 downgrade must not blindly restore the global UNIQUE
constraints the forward migration deliberately removed.

Cross-user duplicate dedupe_hash / paper_canonical_id rows are legal after 0038
(request_repository.py writes them via ON CONFLICT (user_id, ...)), so an
unconditional create_unique_constraint would abort on a Postgres unique
violation. The downgrade now checks first: it refuses cleanly when duplicates
exist and restores the constraints only on a conflict-free table.

The guard logic is exercised with a mocked alembic ``op`` (a real Postgres round
trip is a CI-only concern), loading the versioned migration file directly since
its module name starts with a digit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.no_network

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "app/db/alembic/versions/0038_scope_request_dedupe_by_user.py"
)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0038", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_op(duplicate_groups: int) -> MagicMock:
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.scalar.return_value = duplicate_groups
    return fake_op


def test_downgrade_refuses_when_cross_user_duplicates_exist() -> None:
    module = _load_migration()
    fake_op = _fake_op(duplicate_groups=3)

    with patch.object(module, "op", fake_op), pytest.raises(RuntimeError, match="past 0038"):
        module.downgrade()

    # It must refuse before touching the schema (transaction stays clean).
    fake_op.drop_index.assert_not_called()
    fake_op.create_unique_constraint.assert_not_called()


def test_downgrade_restores_constraints_on_conflict_free_table() -> None:
    module = _load_migration()
    fake_op = _fake_op(duplicate_groups=0)

    with patch.object(module, "op", fake_op):
        module.downgrade()

    # No duplicates -> full revert: drop both per-user indexes, restore both
    # global unique constraints.
    assert fake_op.drop_index.call_count == 2
    assert fake_op.create_unique_constraint.call_count == 2


def test_downgrade_treats_null_count_as_no_duplicates() -> None:
    module = _load_migration()
    fake_op = _fake_op(duplicate_groups=None)  # scalar() may return None

    with patch.object(module, "op", fake_op):
        module.downgrade()

    assert fake_op.create_unique_constraint.call_count == 2
