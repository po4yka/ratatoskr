from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"
EXCLUDED_GLOBS = [
    "!app/di/**",
    "!app/cli/**",
    "!app/bootstrap/**",
    "!app/db/migrations/**",
    # Taskiq tasks are runtime entrypoints (analogous to CLI/bootstrap); each
    # task constructs its own LLM/embedding clients from the loaded settings.
    "!app/tasks/**",
]
PATTERNS = [
    "DatabaseSessionManager(",
    "LLMClientFactory.create_from_config(",
    "ContentScraperFactory.create_from_config(",
    "ResponseFormatter(",
    "LocalTopicSearchService(",
    "SummaryEmbeddingGenerator(",
    "QdrantVectorStore(",
]
FORMATTER_PRIVATE_PATTERNS = {
    "response_formatter.sender": re.compile(r"\b[\w.]*response_formatter\.sender\b"),
    "response_formatter.notifications": re.compile(r"\b[\w.]*response_formatter\.notifications\b"),
    "response_formatter.summaries": re.compile(r"\b[\w.]*response_formatter\.summaries\b"),
    "response_formatter.database": re.compile(r"\b[\w.]*response_formatter\.database\b"),
    "response_formatter._summary_presenter": re.compile(
        r"\b[\w.]*response_formatter\._summary_presenter\b"
    ),
    "response_formatter._notification_formatter": re.compile(
        r"\b[\w.]*response_formatter\._notification_formatter\b"
    ),
    "response_formatter._response_sender": re.compile(
        r"\b[\w.]*response_formatter\._response_sender\b"
    ),
    "response_formatter._safe_reply_func": re.compile(
        r"\b[\w.]*response_formatter\._safe_reply_func\b"
    ),
    "response_formatter._reply_json_func": re.compile(
        r"\b[\w.]*response_formatter\._reply_json_func\b"
    ),
}
FORMATTER_PRIVATE_MODULE_PATTERNS = [
    re.compile(r"from app\.adapters\.external\.formatting\._response_sender_"),
    re.compile(r"import app\.adapters\.external\.formatting\._response_sender_"),
    re.compile(
        r"from app\.adapters\.external\.formatting\.summary\.(presenter_context|summary_blocks|followup_presenters|structured_summary_flow)"
    ),
    re.compile(
        r"import app\.adapters\.external\.formatting\.summary\.(presenter_context|summary_blocks|followup_presenters|structured_summary_flow)"
    ),
]


def _run_rg(
    *, pattern: str, path: str = "app", fixed: bool = False, globs: list[str] | None = None
):
    glob_args: list[str] = []
    for glob in globs or []:
        glob_args.extend(["--glob", glob])
    cmd = ["rg", "-n"]
    if fixed:
        cmd.append("-F")
    cmd.extend([pattern, path, *glob_args])
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_python(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg is required for architecture guard")
def test_runtime_resource_construction_is_centralized_in_app_di() -> None:
    """Production runtime resources should only be assembled in app/di or CLI binaries."""
    for pattern in PATTERNS:
        result = _run_rg(pattern=pattern, fixed=True, globs=EXCLUDED_GLOBS)
        assert result.returncode in (0, 1)
        assert result.stdout.strip() == "", (
            f"found forbidden runtime construction for {pattern!r}:\n{result.stdout}"
        )


def test_formatter_private_surfaces_are_not_used_outside_formatting_package() -> None:
    """Production code should use ResponseFormatter's public API only."""
    excluded = {
        PROJECT_ROOT / "app" / "adapters" / "external" / "response_formatter.py",
    }

    for path in APP_ROOT.rglob("*.py"):
        if "app/adapters/external/formatting/" in path.as_posix():
            continue
        if path in excluded:
            continue

        text = path.read_text()
        offenders = [
            label for label, pattern in FORMATTER_PRIVATE_PATTERNS.items() if pattern.search(text)
        ]
        assert offenders == [], f"found forbidden formatter surface usage in {path}:\n" + "\n".join(
            offenders
        )


def test_formatter_private_modules_are_not_imported_outside_formatting_package() -> None:
    """Production code should import only formatter public modules/protocols."""
    for path in APP_ROOT.rglob("*.py"):
        if "app/adapters/external/formatting/" in path.as_posix():
            continue
        if path == APP_ROOT / "adapters" / "external" / "response_formatter.py":
            continue
        text = path.read_text()
        offenders = [
            pattern.pattern for pattern in FORMATTER_PRIVATE_MODULE_PATTERNS if pattern.search(text)
        ]
        assert offenders == [], (
            f"found forbidden formatter private import in {path}:\n" + "\n".join(offenders)
        )


def test_url_processor_keeps_repository_assembly_in_di_layer() -> None:
    """URLProcessor should receive repositories from DI instead of composing SQLite adapters."""
    path = APP_ROOT / "adapters" / "content" / "url_processor.py"
    text = path.read_text()

    forbidden_fragments = (
        "MessagePersistence(",
        "SummaryRepositoryAdapter(",
        "from app.infrastructure.persistence.message_persistence import",
        "from app.infrastructure.persistence.repositories.summary_repository import",
    )
    offenders = [fragment for fragment in forbidden_fragments if fragment in text]
    assert offenders == [], f"found forbidden repository assembly in {path}:\n" + "\n".join(
        offenders
    )


def test_formatter_concrete_root_modules_remain_thin_shells() -> None:
    """Concrete formatter roots should only expose construction and public delegation."""
    module_expectations = {
        APP_ROOT / "adapters" / "external" / "formatting" / "response_sender.py": (
            "ResponseSenderImpl"
        ),
        APP_ROOT / "adapters" / "external" / "formatting" / "summary_presenter.py": (
            "SummaryPresenterImpl"
        ),
    }

    for path, class_name in module_expectations.items():
        tree = _parse_python(path)
        module_functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
        assert module_functions == [], (
            f"{path} should not define module-level helpers: {module_functions}"
        )

        classes = [
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        assert len(classes) == 1, f"{path} should define exactly one {class_name}"
        methods = [
            node.name
            for node in classes[0].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        private_methods = [name for name in methods if name.startswith("_") and name != "__init__"]
        assert private_methods == [], (
            f"{path} should not define private helper methods in {class_name}: {private_methods}"
        )


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg is required for architecture guard")
def test_removed_repository_port_layer_and_container_are_not_used() -> None:
    patterns = [
        "from app.adapters.repository_ports",
        "import app.adapters.repository_ports",
        "from app.di.container import",
        "Container(",
    ]
    for pattern in patterns:
        result = _run_rg(pattern=pattern, fixed=True)
        assert result.returncode in (0, 1)
        assert result.stdout.strip() == "", (
            f"found removed architecture surface for {pattern!r}:\n{result.stdout}"
        )


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg is required for architecture guard")
def test_core_workflows_do_not_import_legacy_search_service_modules() -> None:
    patterns = [
        r"from app\.services\.(topic_search|summary_embedding_generator|vector_search_service|hybrid_search_service|related_reads_service|topic_search_utils)",
        r"import app\.services\.(topic_search|summary_embedding_generator|vector_search_service|hybrid_search_service|related_reads_service|topic_search_utils)",
    ]
    globs = [
        "!app/services/**",
        "!app/cli/**",
        "!app/application/services/topic_search_utils.py",
    ]
    for pattern in patterns:
        result = _run_rg(pattern=pattern, globs=globs)
        assert result.returncode in (0, 1)
        assert result.stdout.strip() == "", (
            f"found legacy search-service import still in production code for {pattern!r}:\n"
            f"{result.stdout}"
        )


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg is required for architecture guard")
def test_core_workflows_do_not_construct_sqlite_repositories_outside_di() -> None:
    result = _run_rg(
        pattern=r"Sqlite[A-Za-z]+RepositoryAdapter\(",
        path="app",
        globs=[
            "app/api/services/**",
            "app/adapters/telegram/**",
            "app/application/**",
            "app/api/background_processor.py",
            # Known-debt: services that still construct repos internally
            "!app/api/services/collection_service.py",
            "!app/api/services/admin_read_service.py",
            "!app/api/services/user_goal_service.py",
            "!app/api/services/highlight_service.py",
            "!app/api/services/import_export_service.py",
            "!app/api/services/custom_digest_service.py",
            # Known-debt: telegram adapters that still construct repos internally
            "!app/adapters/telegram/summary_followup.py",
            "!app/adapters/telegram/callback_action_store.py",
            "!app/adapters/telegram/command_handlers/rss_handler.py",
            "!app/adapters/telegram/command_handlers/listen_handler.py",
            "!app/adapters/telegram/command_handlers/export_command.py",
            "!app/adapters/telegram/command_handlers/backup_handler.py",
            "!app/adapters/telegram/command_handlers/rules_handler.py",
        ],
    )
    assert result.returncode in (0, 1)
    assert result.stdout.strip() == "", (
        "found direct Sqlite repository construction outside app/di in core workflows:\n"
        f"{result.stdout}"
    )


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg is required for architecture guard")
def test_p2_runtime_modules_do_not_import_app_di() -> None:
    result = _run_rg(
        pattern=r"from app\.di|import app\.di|app\.di\.",
        path="app",
        globs=[
            "app/adapters/**",
            "app/api/services/**",
            "app/api/background_processor.py",
            "app/db/**",
            "app/infrastructure/**",
            "!app/bootstrap/**",
        ],
    )
    assert result.returncode in (0, 1)
    assert result.stdout.strip() == "", (
        f"found forbidden app.di import in disallowed runtime package:\n{result.stdout}"
    )


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg is required for architecture guard")
def test_p2_runtime_modules_do_not_use_runtime_builder_shortcuts() -> None:
    patterns = [
        r"build_[A-Za-z0-9_]+repository\(",
        r"build_[A-Za-z0-9_]+dependencies\(",
        r"build_runtime_database\(",
        r"get_current_api_runtime\(",
        r"resolve_api_runtime\(",
        r"build_scheduler_dependencies\(",
    ]
    globs = [
        "app/adapters/**",
        "app/api/services/**",
        "app/api/background_processor.py",
        "app/db/**",
        "app/infrastructure/**",
        "!app/bootstrap/**",
    ]
    for pattern in patterns:
        result = _run_rg(pattern=pattern, path="app", globs=globs)
        assert result.returncode in (0, 1)
        assert result.stdout.strip() == "", (
            f"found forbidden runtime builder shortcut for {pattern!r}:\n{result.stdout}"
        )


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg is required for architecture guard")
def test_production_code_does_not_import_root_application_ports_facade() -> None:
    patterns = [
        "from app.application.ports import",
        "import app.application.ports",
    ]
    for pattern in patterns:
        result = _run_rg(pattern=pattern, path="app", fixed=True)
        assert result.returncode in (0, 1)
        assert result.stdout.strip() == "", (
            f"found forbidden root ports facade import in production code for {pattern!r}:\n"
            f"{result.stdout}"
        )


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg is required for architecture guard")
def test_production_code_does_not_import_response_formatter_root_facade() -> None:
    # Only match non-indented (runtime) imports; indented imports inside
    # TYPE_CHECKING blocks are type-only and acceptable.
    result = _run_rg(
        pattern=r"^from app\.adapters\.external\.response_formatter import ResponseFormatter",
        path="app",
        globs=[
            "!app/di/shared.py",
            "!app/adapters/external/response_formatter.py",
        ],
    )
    assert result.returncode in (0, 1)
    assert result.stdout.strip() == "", (
        "found forbidden ResponseFormatter root facade import outside DI compatibility layer:\n"
        f"{result.stdout}"
    )


# Two earlier guards (test_sqlite_repository_root_modules_are_thin_and_model_free
# and test_private_sqlite_repository_modules_are_not_imported_outside_repository_package)
# enforced a pre-SQLAlchemy "thin shell + _internal helpers" layout under
# app/infrastructure/persistence/sqlite/repositories/. The SQLAlchemy port
# (cdb4c6bf) flattened that package, and the repos are now the direct
# adapter implementations -- they legitimately import app.db.models, expose
# their adapter class with methods, and have no `_*` private siblings to
# guard. The tests were obsolete; deleting them is the architectural
# decision, not a regression.
