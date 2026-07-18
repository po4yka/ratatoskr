"""Guard: CI must never silently drop a real test by ignoring its file.

The unit-test job once hid failing suites -- including the DI-layering guard
``tests/test_runtime_di_architecture.py`` -- behind per-file ``--ignore=`` flags
instead of fixing the underlying violations. Those ignores were removed once the
violations were fixed (production code, not the tests). This guard keeps them
from creeping back: CI may skip whole non-unit suites by directory, but never an
individual ``tests/*.py`` file.
"""

from __future__ import annotations

import re
from pathlib import Path

CI_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

# Non-unit suites that are legitimately run in a separate job or on demand and
# may be skipped by directory in the unit run.
_ALLOWED_IGNORE_PREFIXES = (
    "tests/benchmarks",
    "tests/chaos",
    "tests/integration",
    "tests/stress",
)

# Match only pytest test-path ignores: the value must start with "tests/", which
# excludes unrelated flags like `--ignore-vuln`/`--ignore SFTY-...` used by the
# security scanners.
_IGNORE_RE = re.compile(r"--ignore[=\s]+(tests/\S+)")


def _ci_test_ignores() -> list[str]:
    text = CI_WORKFLOW.read_text()
    return [match.group(1).rstrip("\\").strip("\"'") for match in _IGNORE_RE.finditer(text)]


def test_ci_never_ignores_an_individual_test_file() -> None:
    offenders = [value for value in _ci_test_ignores() if value.endswith(".py")]
    assert offenders == [], (
        "CI --ignore must target a whole suite by directory, never an individual "
        "test file -- ignoring a file hides a real failure instead of fixing it: "
        f"{offenders}"
    )


def test_ci_only_ignores_sanctioned_test_directories() -> None:
    offenders = [
        value for value in _ci_test_ignores() if not value.startswith(_ALLOWED_IGNORE_PREFIXES)
    ]
    assert offenders == [], (
        "Unexpected CI test-suite ignore; only benchmarks/chaos/integration/stress "
        f"may be skipped by directory: {offenders}"
    )


def test_di_layering_guard_is_not_excluded_from_ci() -> None:
    joined = "\n".join(_ci_test_ignores())
    assert "test_runtime_di_architecture" not in joined, (
        "The DI-layering guard must run in CI; fix any violation it catches "
        "rather than ignoring the file."
    )


# ---------------------------------------------------------------------------
# tests/integration/ marker gap
#
# CI runs integration tests in two disjoint jobs:
#   * unit job:        pytest tests/ -m "not integration" --ignore=tests/integration
#   * integration job: pytest tests/ -m "integration"
# A file under tests/integration/ with no `integration` marker is therefore
# collected by NEITHER job -- silently excluded from CI with zero signal. This is
# exactly how the batch-relationship suite (test_batch_relationship_flow.py) was
# lost until commit 97a338b3 re-marked it. These guards keep the whole directory
# honest.
# ---------------------------------------------------------------------------

_INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "tests" / "integration"

# Files under tests/integration/ knowingly NOT selected by the integration job,
# each with a documented reason. Anything else defining tests here MUST carry the
# integration marker. "Silently excluded" is a bug; "explicitly excluded with a
# reason" is a decision.
_JUSTIFIED_UNMARKED: dict[str, str] = {
    # Under-mocked: builds a real Redis client from a MagicMock URL and fails 15
    # cases when run standalone. Marking it would break the integration CI job;
    # it must be repaired (properly isolate Redis) before it can be collected.
    "test_channel_digest_scheduler.py": (
        "known-failing standalone (under-mocked Redis); repair before marking"
    ),
}

_HAS_TESTS_RE = re.compile(r"^\s*(?:async\s+)?def test_|^\s*class Test", re.MULTILINE)


def _integration_test_modules() -> list[Path]:
    return [p for p in sorted(_INTEGRATION_DIR.glob("*.py")) if p.name != "__init__.py"]


def test_every_integration_suite_is_marked_or_justified() -> None:
    offenders = []
    for path in _integration_test_modules():
        text = path.read_text()
        if not _HAS_TESTS_RE.search(text):
            continue  # not a test module (no test functions/classes)
        if "pytest.mark.integration" in text:
            continue  # collected by the integration job
        if path.name in _JUSTIFIED_UNMARKED:
            continue  # excluded on purpose, with a recorded reason
        offenders.append(path.name)
    assert offenders == [], (
        "These tests/integration/ suites define tests but carry no `integration` "
        "marker, so they run in NEITHER CI job (the unit job --ignores the "
        "directory; the integration job selects -m integration). Add "
        "`pytestmark = pytest.mark.integration`, or record a reason in "
        f"_JUSTIFIED_UNMARKED: {offenders}"
    )


def test_batch_relationship_suite_is_collected_by_ci() -> None:
    # Regression lock for the original finding: the batch-relationship integration
    # suite must stay marked so the integration job collects it.
    suite = _INTEGRATION_DIR / "test_batch_relationship_flow.py"
    assert suite.exists(), "batch-relationship integration suite is missing"
    assert "pytest.mark.integration" in suite.read_text(), (
        "test_batch_relationship_flow.py must stay marked `integration` or it is "
        "silently excluded from CI again."
    )


def test_scraper_factory_suite_is_not_excluded_from_ci() -> None:
    # Regression lock: the 22-test scraper-factory suite was once --ignored in CI
    # over two stale provider-count assertions (the chain grew from 8/9 to 10/11
    # rungs when rungs like CloakBrowser were added) instead of updating them. It
    # lives at tests/ root, so the unit job collects it -- it must never be
    # per-file --ignored again; fix the assertions instead.
    joined = "\n".join(_ci_test_ignores())
    assert "test_scraper_factory" not in joined, (
        "The scraper-factory suite must run in CI; update its provider-count "
        "assertions rather than ignoring the file."
    )
    suite = Path(__file__).resolve().parents[1] / "tests" / "test_scraper_factory.py"
    assert suite.exists(), "scraper-factory suite is missing"
