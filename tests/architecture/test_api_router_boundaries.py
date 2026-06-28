from __future__ import annotations

from pathlib import Path

from tests.architecture._import_rules import collect_forbidden_imports


def test_api_router_layer_avoids_direct_persistence_imports() -> None:
    """Routers must stay transport-only and import persistence via services/dependencies."""
    router_root = Path(__file__).resolve().parents[2] / "app" / "api" / "routers"
    violations = collect_forbidden_imports(
        router_root,
        forbidden_prefixes=(
            "app.db.models",
            "app.infrastructure.persistence.repositories",
        ),
        # TODO: known-debt list. endpoints_sessions.py lazy-imports the audit
        # log repository inside a factory function (acceptable lazy-load).
        # github.py/repositories.py still pull GitHubAuthMethod/Repository
        # types from app.db.models; move those enums into the domain layer to
        # remove the ignore.
        # git_mirrors.py imports GitMirror/GitMirrorSource and runs select(GitMirror)
        # queries inline; move the queries into GitMirrorRepository and relocate
        # the GitMirrorSource enum out of app.db.models to remove this ignore.
        # ai_backups.py imports the AiBackupService enum from app.db.models so
        # FastAPI can resolve it as a runtime path-param type; relocate the enum
        # to the domain layer to remove this ignore.
        ignored_path_prefixes=(
            "routers/ai_backups.py",
            "routers/auth/apple.py",
            "routers/auth/endpoints_sessions.py",
            "routers/auth/github.py",
            "routers/auth/magic_link.py",
            "routers/content/search.py",
            "routers/export_integrations.py",
            "routers/repositories.py",
            "routers/git_mirrors.py",
            "routers/user/feed.py",
        ),
    )

    assert violations == []
