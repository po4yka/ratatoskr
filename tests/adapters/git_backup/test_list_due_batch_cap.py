"""GitMirrorRepository.list_due batch cap + fair-rotation ordering.

Regression for the unbounded-query issue: list_due used to select every due
mirror row (order by id, no LIMIT) and materialize them all into memory on each
run. It now:
- caps the result at config.max_mirrors_per_run (0 = unlimited),
- orders least-recently-attempted first (last_attempt_at ASC NULLS FIRST) so the
  cap rotates the batch across successive runs instead of starving the tail.

Hermetic: the session fake captures the compiled statement; no real DB.
"""

from __future__ import annotations

from typing import Any

from app.adapters.git_backup.repository import GitMirrorRepository
from app.config.git_backup import GitBackupConfig


def _make_config(**overrides: Any) -> GitBackupConfig:
    base: dict[str, Any] = {
        "GIT_BACKUP_ENABLED": False,
        "GIT_BACKUP_DATA_PATH": "/tmp/git-mirror-cap-test",
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


class _CapturingSession:
    def __init__(self, sink: dict[str, Any]) -> None:
        self._sink = sink

    async def scalars(self, stmt: Any) -> Any:
        self._sink["stmt"] = stmt

        class _Result:
            def all(self) -> list[Any]:
                return []

        return _Result()


class _CapturingSessionCtx:
    def __init__(self, sink: dict[str, Any]) -> None:
        self._sink = sink

    async def __aenter__(self) -> _CapturingSession:
        return _CapturingSession(self._sink)

    async def __aexit__(self, *args: Any) -> None:
        pass


class _CapturingDB:
    def __init__(self) -> None:
        self.sink: dict[str, Any] = {}

    def session(self) -> _CapturingSessionCtx:
        return _CapturingSessionCtx(self.sink)


def _compiled_sql(stmt: Any) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


class TestListDueBatchCap:
    async def test_default_config_applies_limit(self) -> None:
        db = _CapturingDB()
        repo = GitMirrorRepository(db, _make_config())  # type: ignore[arg-type]

        await repo.list_due()

        sql = _compiled_sql(db.sink["stmt"]).lower()
        assert "limit 1000" in sql

    async def test_custom_config_limit_is_honored(self) -> None:
        db = _CapturingDB()
        repo = GitMirrorRepository(
            db,  # type: ignore[arg-type]
            _make_config(GIT_BACKUP_MAX_MIRRORS_PER_RUN=25),
        )

        await repo.list_due()

        sql = _compiled_sql(db.sink["stmt"]).lower()
        assert "limit 25" in sql

    async def test_zero_config_means_unlimited(self) -> None:
        db = _CapturingDB()
        repo = GitMirrorRepository(
            db,  # type: ignore[arg-type]
            _make_config(GIT_BACKUP_MAX_MIRRORS_PER_RUN=0),
        )

        await repo.list_due()

        sql = _compiled_sql(db.sink["stmt"]).lower()
        assert "limit" not in sql

    async def test_explicit_limit_param_overrides_config(self) -> None:
        db = _CapturingDB()
        repo = GitMirrorRepository(
            db,  # type: ignore[arg-type]
            _make_config(GIT_BACKUP_MAX_MIRRORS_PER_RUN=1000),
        )

        await repo.list_due(limit=5)

        sql = _compiled_sql(db.sink["stmt"]).lower()
        assert "limit 5" in sql

    async def test_orders_by_last_attempt_at_nulls_first_for_fair_rotation(self) -> None:
        db = _CapturingDB()
        repo = GitMirrorRepository(db, _make_config())  # type: ignore[arg-type]

        await repo.list_due()

        sql = _compiled_sql(db.sink["stmt"]).lower()
        assert "order by" in sql
        assert "last_attempt_at asc nulls first" in sql
