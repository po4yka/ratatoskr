from __future__ import annotations

import pytest

from app.config.social import SocialConfig


def test_social_config_threads_defaults_are_read_only() -> None:
    cfg = SocialConfig()
    assert cfg.threads_scopes == ["threads_basic"]
    assert cfg.threads_graph_base_url == "https://graph.threads.net/v1.0"


def test_social_config_parses_threads_scopes_and_rejects_publish_or_reply_management() -> None:
    cfg = SocialConfig(threads_scopes="threads_basic, threads_read_replies")
    assert cfg.threads_scopes == ["threads_basic", "threads_read_replies"]

    with pytest.raises(ValueError, match="must not include publish or reply-management scopes"):
        SocialConfig(threads_scopes="threads_basic threads_content_publish")

    with pytest.raises(ValueError, match="must not include publish or reply-management scopes"):
        SocialConfig(threads_scopes="threads_basic threads_manage_replies")
