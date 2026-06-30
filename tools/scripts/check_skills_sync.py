"""Enforce that agent skill mirrors stay aligned.

A skill is a subdirectory containing a SKILL.md file. .claude/skills and .codex/skills must expose identical skill names; SKILL.md body contents are allowed to diverge because each host may need different trigger wording. .agents/skills is the Codex app import mirror, so it must match .codex/skills exactly. Claude slash commands must also have Codex prompt aliases with the same file stem.
"""

from __future__ import annotations

import filecmp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_SKILLS = ROOT / ".claude" / "skills"
CODEX_SKILLS = ROOT / ".codex" / "skills"
AGENTS_SKILLS = ROOT / ".agents" / "skills"
CLAUDE_COMMANDS = ROOT / ".claude" / "commands"
CODEX_COMMANDS = ROOT / ".codex" / "commands"


def _skill_names(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {
        child.name for child in root.iterdir() if child.is_dir() and (child / "SKILL.md").is_file()
    }


def _relative_files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file()}


def _markdown_stems(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {path.stem for path in root.glob("*.md") if path.name != "README.md"}


def _check_exact_codex_agents_mirror() -> bool:
    codex_files = _relative_files(CODEX_SKILLS)
    agents_files = _relative_files(AGENTS_SKILLS)
    only_codex = sorted(codex_files - agents_files)
    only_agents = sorted(agents_files - codex_files)
    changed = sorted(
        rel
        for rel in codex_files & agents_files
        if not filecmp.cmp(CODEX_SKILLS / rel, AGENTS_SKILLS / rel, shallow=False)
    )
    if not only_codex and not only_agents and not changed:
        print(f".agents/skills mirrors .codex/skills exactly ({len(codex_files)} files).")
        return True

    print(".agents/skills is out of sync with .codex/skills.", file=sys.stderr)
    if only_codex:
        print("\nIn .codex/skills but missing from .agents/skills:", file=sys.stderr)
        for rel in only_codex:
            print(f"  - {rel}", file=sys.stderr)
    if only_agents:
        print("\nIn .agents/skills but missing from .codex/skills:", file=sys.stderr)
        for rel in only_agents:
            print(f"  - {rel}", file=sys.stderr)
    if changed:
        print("\nDifferent between .codex/skills and .agents/skills:", file=sys.stderr)
        for rel in changed:
            print(f"  - {rel}", file=sys.stderr)
    print(
        "\nRefresh the Codex app import mirror with: rsync -a --delete .codex/skills/ .agents/skills/",
        file=sys.stderr,
    )
    return False


def _check_command_aliases() -> bool:
    claude = _markdown_stems(CLAUDE_COMMANDS)
    codex = _markdown_stems(CODEX_COMMANDS)
    only_claude = sorted(claude - codex)
    only_codex = sorted(codex - claude)
    if not only_claude and not only_codex:
        print(f".claude/commands and .codex/commands expose the same {len(claude)} commands.")
        return True

    print("Command aliases are out of sync between .claude and .codex.", file=sys.stderr)
    if only_claude:
        print("\nIn .claude/commands but missing from .codex/commands:", file=sys.stderr)
        for name in only_claude:
            print(f"  - {name}.md", file=sys.stderr)
    if only_codex:
        print("\nIn .codex/commands but missing from .claude/commands:", file=sys.stderr)
        for name in only_codex:
            print(f"  - {name}.md", file=sys.stderr)
    return False


def main() -> int:
    if not CLAUDE_SKILLS.is_dir():
        print(f"Missing directory: {CLAUDE_SKILLS}", file=sys.stderr)
        return 1
    if not CODEX_SKILLS.is_dir():
        print(f"Missing directory: {CODEX_SKILLS}", file=sys.stderr)
        return 1
    if not AGENTS_SKILLS.is_dir():
        print(f"Missing directory: {AGENTS_SKILLS}", file=sys.stderr)
        return 1
    if not CLAUDE_COMMANDS.is_dir():
        print(f"Missing directory: {CLAUDE_COMMANDS}", file=sys.stderr)
        return 1
    if not CODEX_COMMANDS.is_dir():
        print(f"Missing directory: {CODEX_COMMANDS}", file=sys.stderr)
        return 1

    claude = _skill_names(CLAUDE_SKILLS)
    codex = _skill_names(CODEX_SKILLS)

    only_claude = sorted(claude - codex)
    only_codex = sorted(codex - claude)

    skill_sets_match = not only_claude and not only_codex
    mirror_matches = _check_exact_codex_agents_mirror()
    commands_match = _check_command_aliases()
    if skill_sets_match and mirror_matches and commands_match:
        print(f".claude/skills and .codex/skills expose the same {len(claude)} skills.")
        return 0

    if skill_sets_match:
        return 1

    print("Skill directories are out of sync between .claude and .codex.", file=sys.stderr)
    if only_claude:
        print("\nIn .claude/skills but missing from .codex/skills:", file=sys.stderr)
        for name in only_claude:
            print(f"  - {name}", file=sys.stderr)
    if only_codex:
        print("\nIn .codex/skills but missing from .claude/skills:", file=sys.stderr)
        for name in only_codex:
            print(f"  - {name}", file=sys.stderr)
    print(
        "\nEach skill must exist as <name>/SKILL.md under BOTH trees. "
        "Body contents may differ; the directory set must match.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
