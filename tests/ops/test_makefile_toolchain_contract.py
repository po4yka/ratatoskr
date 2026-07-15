from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _target(makefile: str, name: str) -> str:
    return makefile.split(f"{name}:", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]


def test_local_quality_targets_use_lock_backed_toolchain() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    expected_commands = {
        "format": ("ruff format", "isort"),
        "lint": ("ruff check", "check_file_size.py"),
        "type": ("mypy",),
        "test": ("pytest",),
        "test-unit": ("pytest",),
        "test-integration": ("pytest",),
        "test-all": ("pytest",),
        "test-fast": ("pytest",),
        "check-layout": ("check_root_hygiene.py",),
    }

    for target_name, commands in expected_commands.items():
        target = _target(makefile, target_name)
        for command in commands:
            line = next(line for line in target.splitlines() if command in line)
            assert line.lstrip().startswith("uv run --frozen ")
