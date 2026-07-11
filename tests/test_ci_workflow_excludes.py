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
        value
        for value in _ci_test_ignores()
        if not value.startswith(_ALLOWED_IGNORE_PREFIXES)
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
