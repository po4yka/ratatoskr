"""The exact dicts RequestBuilder emits for prompt caching and stream usage.

Both were wrong in ways no test could see, because nothing asserted the emitted
shape -- only that caching had been "applied".
"""

from __future__ import annotations

import pytest

from app.adapters.openrouter.request_builder import RequestBuilder

pytestmark = pytest.mark.no_network


@pytest.fixture
def builder() -> RequestBuilder:
    return RequestBuilder(api_key="test-key")


def test_default_ttl_emits_a_bare_ephemeral_breakpoint(builder: RequestBuilder) -> None:
    assert builder._cache_control_for("ephemeral") == {"type": "ephemeral"}


def test_non_default_ttl_goes_in_the_ttl_field_not_the_type_field(
    builder: RequestBuilder,
) -> None:
    """``type`` is the cache kind; only "ephemeral" is accepted there.

    The anthropic TTL used to be written as ``{"type": "1h"}``. anthropic/* is
    the first fallback model and the long-context model, and caching is on by
    default, so every request to it carried an invalid cache_control: either a
    400 (which chat_response_handler treats as non-retryable, burning that
    cascade rung) or caching silently off and full input-token price per call.
    """
    assert builder._cache_control_for("1h") == {"type": "ephemeral", "ttl": "1h"}


def test_cache_control_shape_reaches_string_and_multipart_content(
    builder: RequestBuilder,
) -> None:
    from_string = builder._add_cache_control({"role": "system", "content": "prompt"}, "1h")
    assert from_string["content"] == [
        {"type": "text", "text": "prompt", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]

    from_parts = builder._add_cache_control(
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        "1h",
    )
    # Only the last text part gets the breakpoint.
    assert from_parts["content"][0] == {"type": "text", "text": "a"}
    assert from_parts["content"][1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_streaming_request_asks_for_usage(builder: RequestBuilder) -> None:
    """Without this, llm_calls.cost_usd and tokens_* are NULL for streamed calls.

    Streaming is the dominant traffic class, so LLM_DAILY_HARD_BUDGET_USD would
    total roughly $0 and never trip.
    """
    from app.adapter_models.llm.llm_models import ChatRequest

    messages = [{"role": "user", "content": "hi"}]

    streamed = builder.build_request_body(
        "m", messages, ChatRequest(messages=messages, stream=True)
    )
    assert streamed["usage"] == {"include": True}

    non_streamed = builder.build_request_body(
        "m", messages, ChatRequest(messages=messages, stream=False)
    )
    # A non-streamed response reports usage on its own.
    assert "usage" not in non_streamed
