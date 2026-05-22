from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.adapters.content.summary_request_factory import log_llm_content_validation
from app.adapters.llm.message_sanitizer import sanitize_messages_for_logging
from app.core.logging_utils import (
    bounded_debug_preview,
    redact_for_logging,
    redact_headers_for_logging,
    redact_url_for_logging,
)


def _cfg(*, debug_payloads: bool) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(debug_payloads=debug_payloads),
        openrouter=SimpleNamespace(
            enable_structured_outputs=True,
            structured_output_mode="json_schema",
            require_parameters=True,
            auto_fallback_structured=True,
        ),
    )


def test_redact_for_logging_removes_tokens_headers_and_private_urls() -> None:
    value = {
        "access_token": "access-secret-value",
        "refreshToken": "refresh-secret-value",
        "api_key": "sk-or-secretsecretsecret",
        "telegram_token": "123456789:ABCDEFghijklmnopqrstuvwxyz123456",
        "github_token": "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "crawl4ai_token": "provider-secret-value",
        "tokens_prompt": 123,
        "headers": {
            "Authorization": "Bearer sk-or-secretsecretsecret",
            "Cookie": "sessionid=secret",
            "X-Api-Key": "fc-secretsecretsecret",
        },
        "source_url": "https://user:pass@example.test/private/path?token=secret&ok=1#frag",
        "message": "Authorization: Bearer sk-or-secretsecretsecret",
    }

    redacted = redact_for_logging(value)
    rendered = str(redacted)

    assert "access-secret-value" not in rendered
    assert "refresh-secret-value" not in rendered
    assert "sk-or-secretsecretsecret" not in rendered
    assert "ABCDEFghijklmnopqrstuvwxyz123456" not in rendered
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in rendered
    assert "provider-secret-value" not in rendered
    assert "sessionid=secret" not in rendered
    assert "/private/path" not in rendered
    assert "user:pass" not in rendered
    assert redacted["source_url"] == "https://example.test/[redacted]?token=%5BREDACTED%5D"
    assert redacted["tokens_prompt"] == 123


def test_redact_headers_for_logging_redacts_auth_cookie_and_api_key() -> None:
    headers = redact_headers_for_logging(
        {
            "Authorization": "Bearer openrouter-secret",
            "Cookie": "sid=secret",
            "X-Api-Key": "provider-secret",
            "Content-Type": "application/json",
        }
    )

    assert headers["Authorization"] == "[REDACTED]"
    assert headers["Cookie"] == "[REDACTED]"
    assert headers["X-Api-Key"] == "[REDACTED]"
    assert headers["Content-Type"] == "application/json"


def test_message_sanitizer_redacts_prompt_content_by_default() -> None:
    messages = [
        {
            "role": "user",
            "content": "Raw article body with https://example.test/private?token=secret",
        }
    ]

    sanitized = sanitize_messages_for_logging(messages)

    assert sanitized == [{"role": "user", "content": "[REDACTED_CONTENT]"}]


def test_bounded_debug_preview_redacts_tokens_urls_and_truncates() -> None:
    preview = bounded_debug_preview(
        "Authorization: Bearer sk-or-secretsecretsecret "
        "https://example.test/private/path?token=secret "
        "x" * 300,
        max_chars=120,
    )

    assert "sk-or-secretsecretsecret" not in preview
    assert "/private/path" not in preview
    assert len(preview) <= 120
    assert preview.endswith("... [truncated]")


def test_llm_content_validation_omits_preview_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.adapters.content.summary_request_factory"):
        log_llm_content_validation(
            cfg=_cfg(debug_payloads=False),
            content_text="Sensitive source body https://example.test/private?token=secret",
            system_prompt="System prompt",
            user_content="User prompt",
            correlation_id="cid-1",
        )

    record = next(item for item in caplog.records if item.message == "llm_content_validation")
    assert not hasattr(record, "text_preview")
    assert not hasattr(record, "debug_text_preview")
    assert record.text_for_summary_len > 0


def test_llm_content_validation_debug_flag_adds_bounded_redacted_preview(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.adapters.content.summary_request_factory"):
        log_llm_content_validation(
            cfg=_cfg(debug_payloads=True),
            content_text="Secret body https://example.test/private?token=secret " + "x" * 500,
            system_prompt="System prompt with api_key=sk-or-secretsecretsecret",
            user_content="User prompt",
            correlation_id="cid-2",
        )

    record = next(item for item in caplog.records if item.message == "llm_content_validation")
    assert hasattr(record, "debug_text_preview")
    assert "Secret body" in record.debug_text_preview
    assert "/private" not in record.debug_text_preview
    assert "sk-or-secretsecretsecret" not in record.debug_system_prompt_preview
    assert len(record.debug_text_preview) <= 200


def test_redact_url_for_logging_preserves_host_only() -> None:
    assert (
        redact_url_for_logging("https://user:pass@example.test/private/path?token=secret&view=1#x")
        == "https://example.test/[redacted]?token=%5BREDACTED%5D"
    )
