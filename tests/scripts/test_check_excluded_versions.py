"""Regression tests for the excluded-version requirements guard.

This guard is the only thing standing between a stale `uv export` artifact and
`pip install -r` pulling a known-malicious/yanked wheel (e.g.
fastapi==0.136.3, osv.dev/MAL-2026-4750). If the guard silently stops catching
violations, the protection is gone -- so these tests prove it both passes clean
inputs and fails on a planted exclusion.
"""

from __future__ import annotations

from tools.scripts.check_excluded_versions import (
    collect_exclusions,
    main,
    scan_requirements,
)

_PYPROJECT_WITH_EXCLUSIONS = """\
[project]
dependencies = [
    "fastapi>=0.128.0,!=0.136.3",
    "httpx>=0.27.0",
]
[project.optional-dependencies]
api = ["uvicorn!=1.2.3"]
[dependency-groups]
dev = ["typing_extensions>=4,!=4.9.9"]
"""


def _write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


def test_collect_exclusions_reads_all_dependency_tables(tmp_path) -> None:
    pyproject = _write(tmp_path / "pyproject.toml", _PYPROJECT_WITH_EXCLUSIONS)

    exclusions = collect_exclusions(pyproject)

    assert exclusions["fastapi"] == {"0.136.3"}
    assert exclusions["uvicorn"] == {"1.2.3"}  # optional-dependencies
    # dependency-groups + name normalisation (underscore -> hyphen).
    assert exclusions["typing-extensions"] == {"4.9.9"}
    assert "httpx" not in exclusions  # no '!=' clause


def test_scan_flags_excluded_pin(tmp_path) -> None:
    req = _write(tmp_path / "requirements.txt", "fastapi==0.136.3\nhttpx==0.27.0\n")

    violations = scan_requirements(req, {"fastapi": {"0.136.3"}})

    assert len(violations) == 1
    assert "fastapi==0.136.3" in violations[0]


def test_scan_ignores_clean_and_non_pin_lines(tmp_path) -> None:
    req = _write(
        tmp_path / "requirements.txt",
        "# a comment\n\nfastapi==0.136.1\nhttpx>=0.27.0\n",
    )

    assert scan_requirements(req, {"fastapi": {"0.136.3"}}) == []


def test_scan_matches_despite_markers_and_name_casing(tmp_path) -> None:
    # Environment markers, case differences, and separator normalisation must
    # not let an excluded pin slip past.
    req = _write(
        tmp_path / "requirements.txt",
        'Fastapi==0.136.3 ; python_version < "3.13"\ntyping_extensions==4.9.9 \\\n',
    )

    violations = scan_requirements(req, {"fastapi": {"0.136.3"}, "typing-extensions": {"4.9.9"}})

    assert len(violations) == 2


def test_main_passes_on_clean_file(tmp_path) -> None:
    clean = _write(tmp_path / "requirements-clean.txt", "fastapi==0.136.1\n")
    assert main([str(clean)]) == 0


def test_main_fails_on_real_excluded_version(tmp_path) -> None:
    # End-to-end against the repo's actual pyproject exclusions: a file pinning
    # the malicious fastapi release must fail.
    planted = _write(tmp_path / "requirements-bad.txt", "fastapi==0.136.3\n")
    assert main([str(planted)]) == 1
