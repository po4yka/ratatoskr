"""Migration 0002 downgrade must not narrow summaries.version back to INTEGER
when millisecond-epoch values are present.

The forward migration widens version INTEGER -> BIGINT because legacy /
live-Pi rows carry ~1.7e12 ms-epoch values that overflow int32. An
unconditional narrowing downgrade reproduces that exact overflow, so the
downgrade now refuses when any value exceeds the int32 max and narrows only on
int32-safe (or empty) data.

The guard is exercised with a mocked alembic ``op`` (a real Postgres round trip
is a CI-only concern); the versioned module is loaded directly since its name
starts with a digit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.no_network

_INT32_MAX = 2_147_483_647
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "app/db/alembic/versions/0002_widen_summary_version_to_bigint.py"
)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_migration_0002", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_op(max_version: int | None) -> MagicMock:
    fake_op = MagicMock()
    fake_op.get_bind.return_value.execute.return_value.scalar.return_value = max_version
    return fake_op


def test_downgrade_refuses_when_version_exceeds_int32() -> None:
    module = _load_migration()
    fake_op = _fake_op(max_version=1_700_000_000_000)  # ms-epoch, > int32

    with patch.object(module, "op", fake_op), pytest.raises(RuntimeError, match="past 0002"):
        module.downgrade()

    # Refuses before touching the column type.
    fake_op.alter_column.assert_not_called()


def test_downgrade_narrows_when_values_fit_int32() -> None:
    module = _load_migration()
    fake_op = _fake_op(max_version=_INT32_MAX)  # boundary value still fits

    with patch.object(module, "op", fake_op):
        module.downgrade()

    fake_op.alter_column.assert_called_once()


def test_downgrade_narrows_on_empty_table() -> None:
    module = _load_migration()
    fake_op = _fake_op(max_version=None)  # MAX() over an empty table

    with patch.object(module, "op", fake_op):
        module.downgrade()

    fake_op.alter_column.assert_called_once()
