from __future__ import annotations

import pytest

from app.config.social import SocialConfig


def test_social_config_threads_defaults_are_read_only() -> None:
    cfg = SocialConfig()
    assert cfg.threads_scopes == ["threads_basic"]
    assert cfg.threads_graph_base_url == "https://graph.threads.net/v1.0"
    assert cfg.instagram_scopes == ["instagram_business_basic"]
    assert cfg.instagram_graph_base_url == "https://graph.instagram.com/v25.0"


def test_social_config_parses_threads_scopes_and_rejects_publish_or_reply_management() -> None:
    cfg = SocialConfig(threads_scopes="threads_basic, threads_read_replies")
    assert cfg.threads_scopes == ["threads_basic", "threads_read_replies"]

    with pytest.raises(ValueError, match="must not include publish or reply-management scopes"):
        SocialConfig(threads_scopes="threads_basic threads_content_publish")

    with pytest.raises(ValueError, match="must not include publish or reply-management scopes"):
        SocialConfig(threads_scopes="threads_basic threads_manage_replies")


def test_social_config_parses_instagram_read_scope_and_rejects_unsupported_scopes() -> None:
    cfg = SocialConfig(instagram_scopes="instagram_business_basic, instagram_business_basic")
    assert cfg.instagram_scopes == ["instagram_business_basic"]

    with pytest.raises(ValueError, match="read-only profile/media scope only"):
        SocialConfig(instagram_scopes="instagram_business_basic instagram_business_content_publish")

    with pytest.raises(ValueError, match="read-only profile/media scope only"):
        SocialConfig(instagram_scopes="instagram_business_manage_messages")
