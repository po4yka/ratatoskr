"""Tests for FieldTheoryConfig."""

from __future__ import annotations

import pytest

from app.config.fieldtheory import FieldTheoryConfig


def test_defaults_load_when_no_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FIELDTHEORY_SYNC_ENABLED",
        "FIELDTHEORY_SYNC_CRON",
        "FIELDTHEORY_BOOKMARKS_DB_PATH",
        "FIELDTHEORY_WIKI_SYNC_CRON",
        "FIELDTHEORY_LIBRARY_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = FieldTheoryConfig()
    assert cfg.enabled is True
    assert cfg.sync_cron == "*/15 * * * *"
    assert cfg.bookmarks_db_path == "/fieldtheory/bookmarks.db"
    assert cfg.wiki_sync_cron == "0 * * * *"
    assert cfg.library_path == "/fieldtheory/library"


def test_env_alias_roundtrip_for_wiki_fields() -> None:
    # FieldTheoryConfig is a BaseModel (not BaseSettings); env vars are wired
    # via Settings._build_nested_from_env using validation_alias. Test the alias
    # round-trip by constructing with the alias keys directly.
    cfg = FieldTheoryConfig.model_validate(
        {
            "FIELDTHEORY_WIKI_SYNC_CRON": "30 */2 * * *",
            "FIELDTHEORY_LIBRARY_PATH": "/srv/ft/wiki",
        }
    )
    assert cfg.wiki_sync_cron == "30 */2 * * *"
    assert cfg.library_path == "/srv/ft/wiki"


def test_wiki_sync_cron_rejects_malformed_expression() -> None:
    with pytest.raises(ValueError, match="wiki sync cron"):
        FieldTheoryConfig(wiki_sync_cron="0 * * *")


def test_library_path_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match="library_path"):
        FieldTheoryConfig(library_path="   ")
