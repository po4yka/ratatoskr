"""Shared fakes for the app.tasks.github_sync suites.

The stars-sync tests split across test_github_sync.py and
test_github_sync_stars.py; the taskiq stubs, config builder and model fakes
they both need live here so neither file owns the other's scaffolding.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Taskiq stub helpers (copied from test_digest_task pattern)
# ---------------------------------------------------------------------------


def _stub_taskiq(monkeypatch):
    """Stub taskiq and taskiq_redis so imports work without Redis."""
    for mod_name in (
        "taskiq",
        "taskiq.abc",
        "taskiq.abc.schedule_source",
        "taskiq.scheduler",
        "taskiq.scheduler.scheduled_task",
        "taskiq.message",
        "taskiq_redis",
    ):
        if mod_name not in sys.modules:
            monkeypatch.setitem(sys.modules, mod_name, types.ModuleType(mod_name))

    taskiq_mod = sys.modules["taskiq"]
    monkeypatch.setattr(taskiq_mod, "AsyncBroker", object, raising=False)
    monkeypatch.setattr(taskiq_mod, "TaskiqDepends", lambda fn, **_kw: None, raising=False)
    monkeypatch.setattr(taskiq_mod, "TaskiqMiddleware", object, raising=False)
    monkeypatch.setattr(taskiq_mod, "InMemoryBroker", MagicMock, raising=False)
    monkeypatch.setattr(taskiq_mod, "TaskiqScheduler", MagicMock, raising=False)

    msg_mod = sys.modules["taskiq.message"]
    monkeypatch.setattr(msg_mod, "TaskiqMessage", object, raising=False)

    sched_task_mod = sys.modules["taskiq.scheduler.scheduled_task"]
    monkeypatch.setattr(sched_task_mod, "ScheduledTask", MagicMock, raising=False)

    source_mod = sys.modules["taskiq.abc.schedule_source"]
    monkeypatch.setattr(source_mod, "ScheduleSource", object, raising=False)

    tkr_mod = sys.modules["taskiq_redis"]
    monkeypatch.setattr(tkr_mod, "RedisStreamBroker", MagicMock, raising=False)
    monkeypatch.setattr(tkr_mod, "RedisAsyncResultBackend", MagicMock, raising=False)


def _evict_task_modules():
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# Minimal config builder
# ---------------------------------------------------------------------------


def _build_cfg(
    *,
    sync_enabled: bool = True,
    llm_concurrency: int = 2,
    llm_daily_budget: int = 100,
    sync_star_lists: bool = False,
):
    return SimpleNamespace(
        github=SimpleNamespace(
            sync_enabled=sync_enabled,
            sync_cron="0 2 * * *",
            llm_concurrency=llm_concurrency,
            llm_daily_budget=llm_daily_budget,
            sync_batch_size=50,
            full_sync_interval_days=7,
            # Off by default here so the star-sync tests need no GraphQL fake;
            # the list-mirror tests below opt in explicitly.
            sync_star_lists=sync_star_lists,
        ),
        digest=SimpleNamespace(enabled=False, digest_times=[], timezone="UTC"),
        rss=SimpleNamespace(enabled=False, poll_interval_minutes=30),
        signal_ingestion=SimpleNamespace(enabled=False, any_enabled=False),
        openrouter=SimpleNamespace(api_key="k", model="m", fallback_models=[]),
        telegram=SimpleNamespace(api_id=1, api_hash="h", bot_token="t:tok", allowed_user_ids=[123]),
    )


# ---------------------------------------------------------------------------
# Fake DB / model helpers
# ---------------------------------------------------------------------------

from datetime import UTC, datetime


def _make_integration(
    *,
    user_id: int = 42,
    status: str = "active",
    last_synced_at=None,
    last_full_sync_at=None,
    notified_needs_reauth_at=None,
):
    from app.db.models.repository import GitHubIntegrationStatus

    integ = MagicMock()
    integ.id = 1
    integ.user_id = user_id
    integ.status = GitHubIntegrationStatus(status)
    integ.encrypted_token = b"fake-token"
    integ.last_synced_at = last_synced_at
    integ.last_full_sync_at = last_full_sync_at
    integ.notified_needs_reauth_at = notified_needs_reauth_at
    return integ


def _make_repo(
    *,
    github_id: int = 1001,
    user_id: int = 42,
    content_hash: str | None = None,
    pending_analysis: bool = False,
):
    from app.db.models.repository import RepoSource

    repo = MagicMock()
    repo.id = github_id
    repo.github_id = github_id
    repo.user_id = user_id
    repo.content_hash = content_hash
    repo.pending_analysis = pending_analysis
    repo.is_starred = True
    repo.description = "desc"
    repo.topics_json = []
    repo.readme_excerpt = ""
    repo.created_at_github = datetime(2020, 1, 1, tzinfo=UTC)
    repo.source = RepoSource.STARRED
    return repo


def _make_starred_item(*, github_id: int = 1001, name: str = "repo"):
    from app.adapters.github.types import GitHubOwnerDTO, RepositoryDTO, StarredItem

    owner = GitHubOwnerDTO(login="owner", id=99, type="User")
    repo_dto = RepositoryDTO(
        id=github_id,
        name=name,
        full_name=f"owner/{name}",
        owner=owner,
        description="desc",
        homepage=None,
        language="Python",
        topics=[],
        stargazers_count=10,
        forks_count=0,
        watchers_count=0,
        default_branch="main",
        license=None,
        archived=False,
        fork=False,
        is_template=False,
        pushed_at=datetime(2024, 1, 1, tzinfo=UTC),
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        html_url=f"https://github.com/owner/{name}",
    )
    return StarredItem(
        starred_at=datetime(2024, 6, 1, tzinfo=UTC),
        repo=repo_dto,
    )


# ---------------------------------------------------------------------------
# Async iterator helper
# ---------------------------------------------------------------------------


async def _async_iter(items):
    for item in items:
        yield item


class _RecordingMetric:
    def __init__(self) -> None:
        self.inc_calls: list[tuple[dict[str, str], float]] = []
        self.set_calls: list[tuple[dict[str, str], float]] = []

    def labels(self, **labels: str) -> _RecordingMetric:
        child = _RecordingMetric()
        child.inc_calls = self.inc_calls
        child.set_calls = self.set_calls
        child._labels = labels
        return child

    def inc(self, amount: float = 1.0) -> None:
        self.inc_calls.append((getattr(self, "_labels", {}), amount))

    def set(self, value: float) -> None:
        self.set_calls.append((getattr(self, "_labels", {}), value))
