"""Tests for the AI backup client factory."""

from __future__ import annotations

import pytest

from app.adapters.ai_backup.chatgpt_client import ChatGptClient
from app.adapters.ai_backup.claude_client import ClaudeClient
from app.adapters.ai_backup.client_factory import build_client
from app.config.ai_backup import AiBackupConfig
from app.db.models.ai_backup import AiBackupService


def test_build_chatgpt() -> None:
    client = build_client(
        AiBackupService.CHATGPT, object(), object(), AiBackupConfig(), last_backed_up_at=None
    )
    assert isinstance(client, ChatGptClient)


def test_build_claude() -> None:
    client = build_client(
        AiBackupService.CLAUDE, object(), object(), AiBackupConfig(), last_backed_up_at=None
    )
    assert isinstance(client, ClaudeClient)


def test_unknown_service_raises() -> None:
    with pytest.raises(ValueError, match="Unknown AiBackupService"):
        build_client("nope", object(), object(), AiBackupConfig(), last_backed_up_at=None)  # type: ignore[arg-type]
