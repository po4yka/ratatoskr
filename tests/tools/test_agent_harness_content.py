from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.core.summary_schema import SummaryModel

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOTS = (".claude/skills", ".codex/skills", ".agents/skills")
ROOT_AGENT_GUIDES = ("AGENTS.md", "CLAUDE.md")


def _skill(host_root: str, name: str) -> str:
    return (ROOT / host_root / name / "SKILL.md").read_text()


def _guide(name: str) -> str:
    return (ROOT / name).read_text()


def test_root_agent_guides_are_standalone_safe() -> None:
    unavailable_parent_dependencies = (
        "../AGENTS.md",
        "../CLAUDE.md",
        "../.claude/skills/",
        "openapi-bump-cross-repo",
        "local-stack-up",
        "frost-token-mirror",
        "workspace-status",
    )

    for guide_name in ROOT_AGENT_GUIDES:
        guide = _guide(guide_name)

        assert "self-contained for repository-local work" in guide
        assert "its absence must not block work in this repository" in guide
        assert all(item not in guide for item in unavailable_parent_dependencies)


def test_codex_app_skill_tree_is_described_as_a_tracked_mirror() -> None:
    agents = _guide("AGENTS.md")
    claude = _guide("CLAUDE.md")

    assert "regular tracked directory `.agents/skills/`" in agents
    assert "checked-in Codex app import mirror" in claude
    assert "Codex import symlink" not in claude


def test_summarize_rag_flag_is_documented_as_active_until_t6() -> None:
    claude = _guide("CLAUDE.md")

    assert "active opt-in transitional flag" in claude
    assert "scheduled for removal at the future T6 cutover" in claude
    assert "retired at the T6 cutover" not in claude


def test_ponytail_uses_repo_local_request_scoped_skills() -> None:
    settings = json.loads((ROOT / ".claude/settings.json").read_text())

    assert "ponytail@ponytail" not in settings.get("enabledPlugins", {})

    for host_root in SKILL_ROOTS:
        ponytail = _skill(host_root, "ponytail")
        help_card = _skill(host_root, "ponytail-help")

        assert "Apply Ponytail only to the request that triggered this skill" in ponytail
        assert "later requests use normal behavior" in ponytail
        assert "ACTIVE EVERY RESPONSE" not in ponytail
        assert "Level persists until changed or session end" not in ponytail
        assert "No persistent mode or flag is stored" in help_card
        assert "ponytail-audit" in help_card
        assert "ponytail-debt" in help_card
        assert "PONYTAIL_DEFAULT_MODE" not in help_card
        assert "/plugin" not in help_card

    for command_root in (".claude/commands", ".codex/commands"):
        command = (ROOT / command_root / "ponytail.md").read_text()
        assert "current request only" in command


def test_migration_skills_distinguish_dry_run_from_apply() -> None:
    for host_root in SKILL_ROOTS:
        migration = _skill(host_root, "alembic-migrations")
        inspection = _skill(host_root, "inspecting-database")

        assert "default command is a dry-run" in migration
        assert "python -m app.cli.migrate_db --apply" in migration
        assert "python -m app.cli.migrate_db --check" in migration
        assert "apply it explicitly" in inspection
        assert "python -m app.cli.migrate_db --apply" in inspection


def test_pi_deploy_skills_require_explicit_migration_apply() -> None:
    for host_root in SKILL_ROOTS:
        deployment = _skill(host_root, "pi-deploy")

        assert "make pi-migrate APPLY=1" in deployment
        assert "make pi-rollback SERVICE=ratatoskr" in deployment
        assert "Migrations are not applied as an automatic restart side effect" in deployment


def test_task_board_skill_uses_issue_notes_as_only_task_storage() -> None:
    for host_root in SKILL_ROOTS:
        task_board = _skill(host_root, "repo-task-board")

        assert "Create `docs/tasks/issues/<kebab-case-slug>.md`" in task_board
        assert "Complete or drop a task by deleting" in task_board
        assert "Choose the right file: `backlog.md`" not in task_board
        assert "docs/ROADMAP_PRIORITIES.md" not in task_board


def test_operational_skills_reference_current_architecture() -> None:
    for host_root in SKILL_ROOTS:
        telegram = _skill(host_root, "adding-telegram-command")
        frontend = _skill(host_root, "web-frontend-dev")
        scraper = _skill(host_root, "scraper-chain-debugging")
        digest = _skill(host_root, "digest-subsystem-ops")
        api_debugging = _skill(host_root, "debugging-apis")
        vector = _skill(host_root, "vector-index-sync")

        assert "TelegramCommandContribution" in telegram
        assert "app/di/telegram_commands.py" in telegram
        assert "app/adapters/telegram/commands.py" not in telegram
        assert "../ratatoskr-web" in frontend
        assert "Do not create or edit a local `web/`" in frontend
        assert "winning_provider" in scraper and "attempt_log" in scraper
        assert "one row per request" in scraper
        assert "ratatoskr.digest.run" in digest
        assert "Taskiq scheduler process" in digest
        assert "external `ratatoskr-web`" in digest
        assert "app/adapters/external/firecrawl/parsing.py" in api_debugging
        assert "app/adapters/external/firecrawl_parser.py" not in api_debugging
        assert "QdrantSummaryIndexAdapter" in vector
        assert "app/application/services/summary_embedding_generator.py" in vector
        assert "EMBEDDING_PROVIDER=voyage" in vector


def test_summary_validation_skills_distinguish_strict_and_compat_modes() -> None:
    for host_root in SKILL_ROOTS:
        validation = _skill(host_root, "validating-summaries")
        testing = _skill(host_root, "testing-workflows")

        assert "Strict provider-schema validation" in validation
        assert "Compatibility shaping" in validation
        assert "get_summary_json_schema()" in validation
        assert "validate_and_shape_summary()" in validation
        assert "validate_summary_json" not in validation
        assert "strict provider schema" in testing
        assert "tolerant compatibility mapper" in testing


def test_bundled_summary_validators_enforce_their_documented_modes(tmp_path: Path) -> None:
    complete = tmp_path / "complete.json"
    incomplete = tmp_path / "incomplete.json"
    complete.write_text(
        json.dumps(
            SummaryModel(
                summary_250="Short summary.",
                summary_1000="Longer summary with details.",
                tldr="TLDR version.",
            ).model_dump(mode="json")
        )
    )
    incomplete.write_text(
        json.dumps(
            {
                "summary_250": "Short summary.",
                "summary_1000": "Longer summary with details.",
                "tldr": "TLDR version.",
            }
        )
    )

    for host_root in SKILL_ROOTS:
        scripts = ROOT / host_root / "validating-summaries" / "scripts"
        strict = scripts / "validate-summary.py"
        compatibility = scripts / "validate-with-project.py"

        assert (
            subprocess.run(
                [sys.executable, str(strict), str(complete)], cwd=ROOT, check=False
            ).returncode
            == 0
        )
        assert (
            subprocess.run(
                [sys.executable, str(strict), str(incomplete)], cwd=ROOT, check=False
            ).returncode
            == 1
        )
        assert (
            subprocess.run(
                [sys.executable, str(compatibility), str(incomplete)],
                cwd=ROOT,
                check=False,
            ).returncode
            == 0
        )
