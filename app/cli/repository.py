"""CLI tooling to exercise the GitHub repository ingestion flow locally."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app.adapters.github.url_patterns import is_github_repo_url, parse_github_repo_url
from app.cli._runtime import prepare_config as _prepare_config
from app.core.logging_utils import generate_correlation_id, get_logger, setup_json_logging
from app.db.models.repository import GitHubIntegrationStatus, Repository, UserGitHubIntegration
from app.di.database import build_runtime_database

logger = get_logger(__name__)

__all__ = ["main", "run_repository_cli"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Run the GitHub repository ingestion flow locally for testing",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--url",
        required=True,
        help="GitHub repository URL (e.g. https://github.com/owner/repo).",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Telegram user_id; must have an active UserGitHubIntegration row.",
    )
    parser.add_argument(
        "--json-path",
        type=Path,
        help="Write the final RepoAnalysis JSON to this file.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Override the configured log level for this session.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Path to a .env file containing environment variables for the run.",
    )
    parser.add_argument(
        "--force-reanalyze",
        action="store_true",
        default=False,
        help="Bypass content_hash short-circuit and always re-run the LLM analysis.",
    )
    parser.add_argument(
        "--correlation-id",
        help="Correlation ID for tracing; generated automatically if not supplied.",
    )
    return parser.parse_args(argv)


async def run_repository_cli(args: argparse.Namespace) -> None:
    """Execute the GitHub repository ingestion flow based on parsed CLI arguments."""
    # 1. Validate URL
    url = args.url.strip()
    if not is_github_repo_url(url):
        print(
            f"Error: '{url}' is not a valid GitHub repository URL. "
            "Expected format: https://github.com/owner/repo",
            file=sys.stderr,
        )
        raise SystemExit(2)

    cfg = _prepare_config(args)
    setup_json_logging(cfg.runtime.log_level)

    correlation_id = args.correlation_id or generate_correlation_id()
    logger.info("cli_repository_start", extra={"cid": correlation_id, "url": url})

    # 2. Build DB
    db = build_runtime_database(cfg, migrate=True)

    # 3. Resolve UserGitHubIntegration
    async with db.session() as session:
        stmt = select(UserGitHubIntegration).where(UserGitHubIntegration.user_id == args.user_id)
        result = await session.execute(stmt)
        integration = result.scalar_one_or_none()

    if integration is None or integration.status != GitHubIntegrationStatus.ACTIVE:
        print(
            f"Error: No active GitHub integration for user_id={args.user_id}. "
            "Connect GitHub first via `python -m app.cli.repository` setup "
            "or POST /v1/auth/github/pat.",
            file=sys.stderr,
        )
        raise SystemExit(3)

    # 4. Wire dependencies
    from app.adapters.github.platform_extractor import GitHubPlatformExtractor
    from app.adapters.llm import LLMClientFactory
    from app.agents.repo_analysis_agent import RepoAnalysisAgent
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase
    from app.infrastructure.embedding.embedding_factory import create_embedding_service
    from app.infrastructure.embedding.repository_embedding import RepositoryEmbeddingGenerator
    from app.infrastructure.persistence.repositories.repository_analysis_repository import (
        RepositoryAnalysisRepositoryAdapter,
    )

    llm_client = LLMClientFactory.create_from_config(cfg)
    embedding_service = create_embedding_service(cfg.embedding)
    qdrant_store: object | None = None
    try:
        from app.di.shared import build_qdrant_vector_store

        qdrant_store = build_qdrant_vector_store(cfg)
    except Exception:
        qdrant_store = None

    embedding_gen = RepositoryEmbeddingGenerator(
        embedding_service=embedding_service,
        qdrant_store=qdrant_store,  # type: ignore[arg-type]
        db=db,
        environment=cfg.vector_store.environment,
        user_scope=cfg.vector_store.user_scope,
    )
    agent = RepoAnalysisAgent(llm_service=llm_client)
    repository_repo = RepositoryAnalysisRepositoryAdapter(db)
    analyze_use_case = AnalyzeRepositoryUseCase(
        repository_repo=repository_repo,
        agent=agent,
        embedding_gen=embedding_gen,
    )

    # When --force-reanalyze is set, wrap the use case so force=True is always passed.
    if args.force_reanalyze:
        _inner_analyze = analyze_use_case.analyze

        async def _forced_analyze(
            repository_id: int,
            *,
            force: bool = False,
            correlation_id: str,
            chosen_lang: str = "en",
        ) -> object:
            from typing import Literal

            _lang: Literal["en", "ru"] = "en" if chosen_lang != "ru" else "ru"
            return await _inner_analyze(
                repository_id,
                force=True,
                correlation_id=correlation_id,
                chosen_lang=_lang,
            )

        analyze_use_case.analyze = _forced_analyze  # type: ignore[assignment]

    extractor = GitHubPlatformExtractor(
        db=db,
        github_config=cfg.github,
        analyze_use_case=analyze_use_case,
    )

    # 5. Build request and call extractor
    from app.adapters.content.platform_extraction.models import PlatformExtractionRequest

    request = PlatformExtractionRequest(
        message=None,
        url_text=url,
        normalized_url=url,
        correlation_id=correlation_id,
        user_id=args.user_id,
        mode="pure",
    )

    await extractor.extract(request)

    # 6. Re-load the Repository row and read analysis_json
    parsed = parse_github_repo_url(url)
    assert parsed is not None
    owner, name = parsed
    full_name = f"{owner}/{name}"

    async with db.session() as session:
        stmt_repo = select(Repository).where(
            Repository.user_id == args.user_id,
            Repository.full_name == full_name,
        )
        res = await session.execute(stmt_repo)
        repo_row = res.scalar_one_or_none()

    if repo_row is None:
        print(
            f"Error: Repository row not found after extraction for {full_name!r}.", file=sys.stderr
        )
        raise SystemExit(1)

    analysis_data = repo_row.analysis_json or {}
    analysis_json_str = json.dumps(analysis_data, ensure_ascii=False, indent=2)

    # 7. Output
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(analysis_json_str, encoding="utf-8")
    else:
        print(analysis_json_str)

    cached = repo_row.content_hash is not None and not args.force_reanalyze and bool(analysis_data)
    embedding_refreshed = not cached and bool(analysis_data)
    print(
        f"repository_id={repo_row.id} full_name={repo_row.full_name} "
        f"cached={cached} embedding_refreshed={embedding_refreshed}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m app.cli.repository``."""
    args = parse_args(argv)
    try:
        asyncio.run(run_repository_cli(args))
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1
    except KeyboardInterrupt:  # pragma: no cover - user cancelled
        return 1
    except Exception as exc:
        logger.exception("cli_repository_failed", exc_info=exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
