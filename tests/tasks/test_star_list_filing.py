"""Tests for app.tasks.star_list_filing.

The safety properties matter more than the happy path here: the job writes to the
user's GitHub account, and ``updateUserListsForItem`` is a full overwrite, so a
filing pass that guesses or that touches an already-filed repository is worse
than one that does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from tests.tasks.github_sync_helpers import _evict_task_modules, _stub_taskiq


@dataclass
class _Suggestion:
    list_names: list[str]
    source: str = "knn"
    confidence: float = 0.9


class _Suggester:
    """Records what it was asked and returns a canned answer."""

    def __init__(self, suggestion: _Suggestion) -> None:
        self._suggestion = suggestion
        self.calls: list[dict[str, Any]] = []

    async def suggest(self, **kwargs: Any) -> _Suggestion:
        self.calls.append(kwargs)
        return self._suggestion


class _StarLists:
    def __init__(self, lists: list[Any] | None = None) -> None:
        self._lists = lists if lists is not None else []
        self.writes: list[dict[str, Any]] = []

    async def list_lists(self, user_id: int) -> list[Any]:
        return self._lists

    async def set_repository_lists(self, **kwargs: Any) -> list[str]:
        self.writes.append(kwargs)
        return list(kwargs["list_names"])


def _row(repo_id: int = 7, full_name: str = "owner/repo") -> SimpleNamespace:
    return SimpleNamespace(
        id=repo_id,
        full_name=full_name,
        description="a thing",
        primary_language="Python",
        topics_json=["mcp"],
        readme_excerpt="README body",
    )


def _candidates() -> list[Any]:
    from app.core.star_list_suggestion_schema import StarListCandidate

    return [StarListCandidate(name="Tools", description="dev tools")]


@pytest.mark.asyncio
async def test_confident_suggestion_is_written_back(monkeypatch):
    """A suggested list is applied verbatim and reported as filed."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    from app.tasks.star_list_filing import _file_one

    star_lists = _StarLists()
    runtime = SimpleNamespace(
        suggester=_Suggester(_Suggestion(["Tools"])),
        star_lists=star_lists,
    )

    filed = await _file_one(
        runtime=runtime,
        user_id=42,
        row=_row(),
        candidate_lists=_candidates(),
    )

    assert filed is True
    assert len(star_lists.writes) == 1
    write = star_lists.writes[0]
    assert write["repository_id"] == 7
    assert write["user_id"] == 42
    assert write["list_names"] == ["Tools"]


@pytest.mark.asyncio
async def test_inconclusive_suggestion_writes_nothing(monkeypatch):
    """An empty suggestion must not reach GitHub at all.

    Writing an empty list would clear the repository's membership rather than
    leave it alone, so "no answer" and "remove from every list" must never be
    conflated.
    """
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    from app.tasks.star_list_filing import _file_one

    star_lists = _StarLists()
    runtime = SimpleNamespace(
        suggester=_Suggester(_Suggestion([], source="none", confidence=0.0)),
        star_lists=star_lists,
    )

    filed = await _file_one(
        runtime=runtime,
        user_id=42,
        row=_row(),
        candidate_lists=_candidates(),
    )

    assert filed is False
    assert star_lists.writes == []


@pytest.mark.asyncio
async def test_suggester_is_offered_only_live_lists(monkeypatch):
    """The candidate set handed to the suggester is the live one, names included."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    from app.tasks.star_list_filing import _file_one

    suggester = _Suggester(_Suggestion(["Tools"]))
    runtime = SimpleNamespace(suggester=suggester, star_lists=_StarLists())

    await _file_one(
        runtime=runtime,
        user_id=42,
        row=_row(),
        candidate_lists=_candidates(),
    )

    asked = suggester.calls[0]
    assert [candidate.name for candidate in asked["available_lists"]] == ["Tools"]
    assert asked["repository_id"] == 7
    assert asked["topics"] == ["mcp"]


@pytest.mark.asyncio
async def test_disabled_flag_short_circuits_the_whole_pass(monkeypatch):
    """The pass is opt-in: disabled means no Redis, no query, no writes."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    import app.tasks.star_list_filing as mod

    async def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("disabled pass must not reach Redis")

    monkeypatch.setattr(mod, "get_redis", _explode)
    cfg = SimpleNamespace(github=SimpleNamespace(star_list_filing_enabled=False))

    summary = await mod.run_filing_pass(cfg, object())

    assert summary.repos_filed == 0
    assert summary.candidates_seen == 0
    assert summary.users_processed == 0


def test_unfiled_predicate_covers_null_and_empty_array(monkeypatch):
    """Both spellings of "no list" must match, since the column is nullable."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    from app.tasks.star_list_filing import _unfiled_predicate

    rendered = str(_unfiled_predicate())
    assert "IS NULL" in rendered
    assert "jsonb_array_length" in rendered
