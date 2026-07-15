"""Enforce structural and semantic alignment across agent harness mirrors."""

from __future__ import annotations

import filecmp
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_SKILLS = ROOT / ".claude" / "skills"
CODEX_SKILLS = ROOT / ".codex" / "skills"
AGENTS_SKILLS = ROOT / ".agents" / "skills"
CLAUDE_COMMANDS = ROOT / ".claude" / "commands"
CODEX_COMMANDS = ROOT / ".codex" / "commands"

HOST_ONLY_MARKDOWN_SECTIONS = {
    Path("ponytail-help/SKILL.md"): ("Update",),
}


def _skill_names(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {
        child.name for child in root.iterdir() if child.is_dir() and (child / "SKILL.md").is_file()
    }


def _relative_files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file()}


def _remove_markdown_section(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*\n.*?(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    return pattern.sub("", text)


def _normalize_host_text(text: str, relative_path: Path) -> str:
    normalized = text.replace(".claude/skills/", ".host/skills/")
    normalized = normalized.replace(".codex/skills/", ".host/skills/")
    normalized = re.sub(r"(?<!\w)[/@](ponytail(?:-[a-z-]+)?)", r"<command>\1", normalized)
    for heading in HOST_ONLY_MARKDOWN_SECTIONS.get(relative_path, ()):
        normalized = _remove_markdown_section(normalized, heading)
    return normalized


def _normalized_file(path: Path, relative_path: Path) -> str | bytes:
    content = path.read_bytes()
    try:
        return _normalize_host_text(content.decode(), relative_path)
    except UnicodeDecodeError:
        return content


def _semantic_tree_differences(
    claude_root: Path, codex_root: Path
) -> tuple[list[Path], list[Path], list[Path]]:
    claude_files = _relative_files(claude_root)
    codex_files = _relative_files(codex_root)
    only_claude = sorted(claude_files - codex_files)
    only_codex = sorted(codex_files - claude_files)
    changed = sorted(
        relative_path
        for relative_path in claude_files & codex_files
        if _normalized_file(claude_root / relative_path, relative_path)
        != _normalized_file(codex_root / relative_path, relative_path)
    )
    return only_claude, only_codex, changed


def _check_semantic_claude_codex_mirror() -> bool:
    only_claude, only_codex, changed = _semantic_tree_differences(
        CLAUDE_SKILLS, CODEX_SKILLS
    )
    if not only_claude and not only_codex and not changed:
        count = len(_relative_files(CLAUDE_SKILLS))
        print(f".claude/skills semantically mirrors .codex/skills ({count} files).")
        return True

    print(".claude/skills is semantically out of sync with .codex/skills.", file=sys.stderr)
    if only_claude:
        print("\nIn .claude/skills but missing from .codex/skills:", file=sys.stderr)
        for relative_path in only_claude:
            print(f"  - {relative_path}", file=sys.stderr)
    if only_codex:
        print("\nIn .codex/skills but missing from .claude/skills:", file=sys.stderr)
        for relative_path in only_codex:
            print(f"  - {relative_path}", file=sys.stderr)
    if changed:
        print("\nSemantically different files:", file=sys.stderr)
        for relative_path in changed:
            print(f"  - {relative_path}", file=sys.stderr)
    print(
        "\nHost skill paths and ponytail command prefixes are normalized; "
        "all other shared content must match.",
        file=sys.stderr,
    )
    return False


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

    semantic_mirror_matches = _check_semantic_claude_codex_mirror()
    mirror_matches = _check_exact_codex_agents_mirror()
    commands_match = _check_command_aliases()
    if semantic_mirror_matches and mirror_matches and commands_match:
        skill_count = len(_skill_names(CLAUDE_SKILLS))
        print(f"All {skill_count} agent skills are synchronized.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
