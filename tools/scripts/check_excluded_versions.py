#!/usr/bin/env python3
"""Fail if any exported requirements file pins a version excluded in pyproject.toml.

`pyproject.toml` excludes known-malicious / yanked releases with PEP 508 `!=`
clauses (e.g. ``fastapi>=0.128.0,!=0.136.3`` for osv.dev/MAL-2026-4750). The
exclusion only protects the uv *resolver* path; a stale committed ``uv export``
artifact (``requirements*.txt``) can still pin the excluded version and be
installed directly via ``pip install -r``. This guard closes that gap: it reads
the exclusions straight from ``pyproject.toml`` (so the list never drifts) and
fails CI if any requirements file pins an excluded ``name==version``.

Usage:
    python tools/scripts/check_excluded_versions.py [requirements_file ...]

With no arguments it checks the standard exported files in the repo root.
Exit code 0 = clean, 1 = a violation (or a usage error).
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUIREMENTS = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-all.txt",
)

# Matches a single PEP 508 `!=` exclusion clause inside a dependency specifier,
# e.g. the `!=0.136.3` in "fastapi>=0.128.0,!=0.136.3".
_NE_CLAUSE = re.compile(r"!=\s*([0-9][^\s,;\]]*)")
# Leading package name (PEP 503 normalised loosely) at the start of a specifier.
_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def collect_exclusions(pyproject: Path) -> dict[str, set[str]]:
    """Return {normalised package name: {excluded version, ...}} from pyproject."""
    data = tomllib.loads(pyproject.read_text())
    project = data.get("project", {})
    specs: list[str] = list(project.get("dependencies", []))
    for extra_deps in (project.get("optional-dependencies", {}) or {}).values():
        specs.extend(extra_deps)
    for group_deps in (data.get("dependency-groups", {}) or {}).values():
        specs.extend(d for d in group_deps if isinstance(d, str))

    exclusions: dict[str, set[str]] = {}
    for spec in specs:
        name_match = _NAME.match(spec)
        ne_versions = _NE_CLAUSE.findall(spec)
        if not name_match or not ne_versions:
            continue
        key = _normalise(name_match.group(1))
        exclusions.setdefault(key, set()).update(ne_versions)
    return exclusions


def scan_requirements(req_file: Path, exclusions: dict[str, set[str]]) -> list[str]:
    """Return a list of human-readable violation strings for one requirements file."""
    violations: list[str] = []
    for raw in req_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip environment markers / hash continuations: "fastapi==0.136.3 ; ..." or trailing "\".
        pinned = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
        if not pinned:
            continue
        name = _normalise(pinned.group(1))
        version = pinned.group(2)
        if version in exclusions.get(name, set()):
            violations.append(f"{req_file.name}: pins excluded {pinned.group(1)}=={version}")
    return violations


def main(argv: list[str]) -> int:
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        print(f"error: {pyproject} not found", file=sys.stderr)
        return 1

    exclusions = collect_exclusions(pyproject)
    if not exclusions:
        print("No '!=' version exclusions declared in pyproject.toml; nothing to check.")
        return 0

    targets = [Path(a) for a in argv] or [REPO_ROOT / name for name in DEFAULT_REQUIREMENTS]
    summary = ", ".join(f"{k}!={sorted(v)}" for k, v in sorted(exclusions.items()))
    print(f"Checking for excluded versions: {summary}")

    all_violations: list[str] = []
    for target in targets:
        if not target.is_file():
            print(f"warning: {target} not found, skipping", file=sys.stderr)
            continue
        all_violations.extend(scan_requirements(target, exclusions))

    if all_violations:
        print("\nERROR: excluded (malicious/yanked) versions found in committed requirements:")
        for v in all_violations:
            print(f"  - {v}")
        print("\nFix: regenerate the exports from uv.lock, e.g. `make lock-uv` or the CI 'uv export' steps.")
        return 1

    print("OK: no excluded versions pinned in any requirements file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
