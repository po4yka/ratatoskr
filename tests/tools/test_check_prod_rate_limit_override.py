from __future__ import annotations

from pathlib import Path

from tools.scripts.check_prod_rate_limit_override import find_forbidden_overrides, main


def test_find_forbidden_overrides_flags_truthy_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text(
        "APP_ENV=production\nexport RATE_LIMIT_REDIS_OVERRIDE='true'\n",
        encoding="utf-8",
    )

    assert find_forbidden_overrides([env_file], allow_missing=False) == [
        f"{env_file}:2: RATE_LIMIT_REDIS_OVERRIDE=true"
    ]


def test_find_forbidden_overrides_allows_false_and_comments(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text(
        "# RATE_LIMIT_REDIS_OVERRIDE=true\nRATE_LIMIT_REDIS_OVERRIDE=false\n",
        encoding="utf-8",
    )

    assert find_forbidden_overrides([env_file], allow_missing=False) == []


def test_main_allows_missing_files_when_requested(tmp_path: Path) -> None:
    assert main(["--allow-missing", str(tmp_path / "missing.env")]) == 0


def test_main_fails_on_truthy_override(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.prod"
    env_file.write_text("RATE_LIMIT_REDIS_OVERRIDE=1\n", encoding="utf-8")

    assert main([str(env_file)]) == 1
