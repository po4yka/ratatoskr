"""Guards migration 0006's downgrade against the multi-user-starred-repo crash.

The downgrade restores the pre-0006 table-level UNIQUE(github_id). That
constraint cannot hold once two users have starred the same repo (the exact
data 0006 exists to allow), so an unconditional ``ADD CONSTRAINT`` aborts the
downgrade with a duplicate-key violation. The restore must therefore be gated
on the data being free of duplicate github_id values.

No live database is needed: we capture the SQL the downgrade emits and assert
the restore is guarded.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "db"
    / "alembic"
    / "versions"
    / "0006_drop_unique_repositories_github_id.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0006", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture_sql(func_name: str) -> str:
    module = _load_migration()
    captured: list[str] = []
    fake_op = MagicMock()
    fake_op.execute.side_effect = lambda sql: captured.append(sql)
    module.op = fake_op
    getattr(module, func_name)()
    return "\n".join(captured)


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).lower()


def test_downgrade_gates_unique_restore_on_absence_of_duplicates() -> None:
    sql = _norm(_capture_sql("downgrade"))

    # The constraint is still restored when the data allows it.
    assert "add constraint repositories_github_id_key unique (github_id)" in sql

    # ...but only after a duplicate-github_id guard, and with a skip path so
    # multi-user data does not abort the downgrade.
    assert "group by github_id" in sql
    assert "having count(*) > 1" in sql
    assert "raise notice" in sql

    # The guard must precede (gate) the restore, not follow it.
    guard_at = sql.index("having count(*) > 1")
    restore_at = sql.index("add constraint repositories_github_id_key unique (github_id)")
    assert guard_at < restore_at, "duplicate guard must gate the ADD CONSTRAINT"


def test_upgrade_still_drops_the_unique_constraint() -> None:
    sql = _norm(_capture_sql("upgrade"))
    assert "drop constraint repositories_github_id_key" in sql


@pytest.mark.parametrize("attr", ["revision", "down_revision"])
def test_revision_chain_unchanged(attr: str) -> None:
    module = _load_migration()
    expected = {"revision": "0006", "down_revision": "0005"}[attr]
    assert getattr(module, attr) == expected
