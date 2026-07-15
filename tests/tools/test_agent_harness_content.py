from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.core.summary_schema import SummaryModel

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOTS = (".claude/skills", ".codex/skills", ".agents/skills")


def _skill(host_root: str, name: str) -> str:
    return (ROOT / host_root / name / "SKILL.md").read_text()


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

        assert subprocess.run(
            [sys.executable, str(strict), str(complete)], cwd=ROOT, check=False
        ).returncode == 0
        assert subprocess.run(
            [sys.executable, str(strict), str(incomplete)], cwd=ROOT, check=False
        ).returncode == 1
        assert subprocess.run(
            [sys.executable, str(compatibility), str(incomplete)],
            cwd=ROOT,
            check=False,
        ).returncode == 0
