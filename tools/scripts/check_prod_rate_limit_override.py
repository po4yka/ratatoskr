"""Fail if production env files enable RATE_LIMIT_REDIS_OVERRIDE."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_ENV_FILES = (
    ".env.production",
    ".env.prod",
    "ops/docker/.env.production",
    "ops/docker/.env.prod",
)


def _strip_optional_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    key, value = stripped.split("=", 1)
    return key.strip(), _strip_optional_quotes(value)


def find_forbidden_overrides(paths: list[Path], *, allow_missing: bool) -> list[str]:
    """Return human-readable findings for env files with a truthy override."""
    findings: list[str] = []
    for path in paths:
        if not path.exists():
            if allow_missing:
                continue
            findings.append(f"{path}: missing")
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            parsed = _parse_env_line(line)
            if parsed is None:
                continue
            key, value = parsed
            if key == "RATE_LIMIT_REDIS_OVERRIDE" and value.strip().lower() in TRUTHY:
                findings.append(f"{path}:{lineno}: RATE_LIMIT_REDIS_OVERRIDE={value}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if production env files enable RATE_LIMIT_REDIS_OVERRIDE=true."
    )
    parser.add_argument(
        "env_files",
        nargs="*",
        default=list(DEFAULT_ENV_FILES),
        help="Production env files to inspect",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Skip missing env files instead of failing",
    )
    args = parser.parse_args(argv)

    findings = find_forbidden_overrides(
        [Path(path) for path in args.env_files],
        allow_missing=args.allow_missing,
    )
    if findings:
        print(
            "Production env files must not set RATE_LIMIT_REDIS_OVERRIDE to a truthy value.",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("Production rate-limit override check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
