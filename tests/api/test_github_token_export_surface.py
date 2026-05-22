from __future__ import annotations

import inspect

from app.api.services.import_export_service import ImportExportService
from app.infrastructure.persistence.repositories.user_content_repository import (
    UserContentRepositoryAdapter,
)


def test_user_export_path_does_not_query_github_token_storage() -> None:
    export_sources = "\n".join(
        [
            inspect.getsource(ImportExportService.export_summaries),
            inspect.getsource(UserContentRepositoryAdapter.async_export_summaries),
        ]
    )

    assert "UserGitHubIntegration" not in export_sources
    assert "encrypted_token" not in export_sources
    assert "token_scopes" not in export_sources
