from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import instructor

from app.adapters.openrouter.openrouter_client import OpenRouterClient
from app.core.summary_schema import SummaryModel


async def test_chat_structured_exposes_instructor_reasks_as_physical_attempts() -> None:
    client = OpenRouterClient(
        api_key="sk-or-v1-test-key-that-passes-validation-1234567890",
        model="primary/model",
    )
    client._oai_client = object()
    client._instructor_async_client = object()
    client._instructor_init_lock = asyncio.Lock()

    completion = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4, cost=0.01)
    )

    def _from_openai(_client, *, mode, hooks):
        assert mode is instructor.Mode.JSON

        async def _create_with_completion(**_kwargs):
            hooks.emit_completion_arguments(model="primary/model")
            hooks.emit_completion_response(completion)
            hooks.emit_parse_error(ValueError("invalid schema"), attempt_number=1)
            hooks.emit_completion_arguments(model="primary/model")
            hooks.emit_completion_response(completion)
            return SummaryModel.model_construct(summary_250="ok"), completion

        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create_with_completion=_create_with_completion)
            )
        )

    with patch("instructor.from_openai", side_effect=_from_openai):
        result = await client._chat_structured_impl(
            [{"role": "user", "content": "summarize"}],
            response_model=SummaryModel,
            max_retries=1,
        )

    assert [attempt["status"] for attempt in result.physical_attempts] == ["error", "ok"]
    assert [attempt["model"] for attempt in result.physical_attempts] == [
        "primary/model",
        "primary/model",
    ]
    assert [attempt["cost_usd"] for attempt in result.physical_attempts] == [0.01, 0.01]
