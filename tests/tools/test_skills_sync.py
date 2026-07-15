from __future__ import annotations

from pathlib import Path

from tools.scripts.check_skills_sync import (
    ROOT,
    _normalize_host_text,
    _semantic_tree_differences,
    main,
)


def test_host_paths_and_command_prefixes_normalize_to_same_content() -> None:
    relative_path = Path("testing-workflows/SKILL.md")
    claude = "Run `.claude/skills/example.py` with `/ponytail-review`."
    codex = "Run `.codex/skills/example.py` with `@ponytail-review`."

    assert _normalize_host_text(claude, relative_path) == _normalize_host_text(
        codex, relative_path
    )


def test_only_declared_host_specific_markdown_section_is_ignored() -> None:
    relative_path = Path("ponytail-help/SKILL.md")
    claude = "# Help\n\n## Update\n\nClaude plugin instructions.\n\n## More\n\nShared.\n"
    codex = "# Help\n\n## More\n\nShared.\n"

    assert _normalize_host_text(claude, relative_path) == _normalize_host_text(
        codex, relative_path
    )


def test_semantic_tree_comparison_detects_content_drift(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    relative_path = Path("example/SKILL.md")
    (claude / relative_path.parent).mkdir(parents=True)
    (codex / relative_path.parent).mkdir(parents=True)
    (claude / relative_path).write_text("Shared behavior.\n")
    (codex / relative_path).write_text("Drifted behavior.\n")

    only_claude, only_codex, changed = _semantic_tree_differences(claude, codex)

    assert only_claude == []
    assert only_codex == []
    assert changed == [relative_path]


def test_repository_agent_skills_are_synchronized() -> None:
    assert main() == 0


def test_skills_sync_workflow_runs_for_source_only_changes() -> None:
    workflow = (ROOT / ".github/workflows/skills-sync.yml").read_text()

    assert "    paths:" not in workflow
