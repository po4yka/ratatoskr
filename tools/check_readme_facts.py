#!/usr/bin/env python3
"""Check README.md facts against ground-truth sources.

Run from the repository root:
    python tools/check_readme_facts.py

Exits 0 on success, 1 on any mismatch.
Update the constants below when intentionally changing the stack topology.
"""

import sys
from pathlib import Path

import yaml

# Update when adding/removing always-on (non-profile) services in docker-compose.yml.
REQUIRED_SERVICES = [
    "migrate",
    "mobile-api",
    "pg-backup",
    "postgres",
    "qdrant",
    "ratatoskr",
    "redis",
    "scheduler",
    "worker",
]

# Update when adding/removing required (uncommented) vars in .env.example.
EXPECTED_REQUIRED_VARS = [
    "ALLOWED_USER_IDS",
    "API_HASH",
    "API_ID",
    "BOT_TOKEN",
    "DATABASE_URL",
    "OPENROUTER_API_KEY",
    "POSTGRES_PASSWORD",
]

REPO_ROOT = Path(__file__).resolve().parent.parent


def check_env_vars(env_example_path: Path) -> list[str]:
    if not env_example_path.exists():
        return [f"MISSING: {env_example_path} does not exist"]
    found: list[str] = []
    for line in env_example_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            var = stripped.split("=", 1)[0].strip()
            if var:
                found.append(var)
    expected_set = set(EXPECTED_REQUIRED_VARS)
    found_set = set(found)
    missing = sorted(expected_set - found_set)
    extra = sorted(found_set - expected_set)
    if not missing and not extra:
        return []
    lines = ["ERROR: .env.example required vars mismatch"]
    lines.append(f"  Expected: {sorted(EXPECTED_REQUIRED_VARS)}")
    lines.append(f"  Found:    {sorted(found)}")
    if missing:
        lines.append(f"  Missing from .env.example: {missing}")
    if extra:
        lines.append(f"  Extra in .env.example (not in allowlist): {extra}")
    return lines


def check_compose_services(compose_path: Path) -> list[str]:
    if not compose_path.exists():
        return [f"MISSING: {compose_path} does not exist"]
    data = yaml.safe_load(compose_path.read_text())
    services = data.get("services", {})
    found = sorted(name for name, svc in services.items() if not svc.get("profiles"))
    expected = sorted(REQUIRED_SERVICES)
    if found == expected:
        return []
    lines = ["ERROR: docker-compose.yml always-on services mismatch"]
    lines.append(f"  Expected: {expected}")
    lines.append(f"  Found:    {found}")
    missing = sorted(set(expected) - set(found))
    extra = sorted(set(found) - set(expected))
    if missing:
        lines.append(f"  Missing from compose: {missing}")
    if extra:
        lines.append(f"  Extra in compose (not in allowlist): {extra}")
    return lines


def main() -> int:
    errors: list[str] = []
    errors += check_env_vars(REPO_ROOT / ".env.example")
    errors += check_compose_services(REPO_ROOT / "ops/docker/docker-compose.yml")
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
