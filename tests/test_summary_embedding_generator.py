from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.ports.search import EmbeddingDependencyUnavailableError
from app.application.services.summary_embedding_generator import SummaryEmbeddingGenerator
from app.core.embedding_text import prepare_text_for_embedding


def _expected_hash(payload: dict, *, max_length: int = 512) -> str:
    metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    text = prepare_text_for_embedding(
        title=metadata.get("title") or payload.get("title"),
        summary_1000=payload.get("summary_1000"),
        summary_250=payload.get("summary_250"),
        tldr=payload.get("tldr"),
        key_ideas=payload.get("key_ideas"),
        topic_tags=payload.get("topic_tags"),
        semantic_boosters=payload.get("semantic_boosters"),
        query_expansion_keywords=payload.get("query_expansion_keywords"),
        semantic_chunks=payload.get("semantic_chunks"),
        max_length=max_length,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def generator_fixture():
    embedding_service = MagicMock()
    embedding_service.get_model_name.return_value = "test-model"
    embedding_service.generate_embedding = AsyncMock(return_value=[0.1, 0.2, 0.3])
    embedding_service.serialize_embedding.return_value = b"serialized"

    embedding_repo = MagicMock()
    embedding_repo.async_get_summary_embedding = AsyncMock(return_value=None)
    embedding_repo.async_create_or_update_summary_embedding = AsyncMock()

    request_repo = MagicMock()
    request_repo.async_get_request_by_id = AsyncMock(return_value=None)

    summary_repo = MagicMock()
    summary_repo.async_get_summary_by_request = AsyncMock(return_value=None)

    generator = SummaryEmbeddingGenerator(
        embedding_repository=embedding_repo,
        request_repository=request_repo,
        summary_repository=summary_repo,
        embedding_service=embedding_service,
        model_version="2.0",
    )
    return generator, embedding_service, embedding_repo, request_repo, summary_repo


@pytest.mark.asyncio
async def test_generate_embedding_for_summary_skips_existing_matching_model(
    generator_fixture,
) -> None:
    generator, embedding_service, embedding_repo, _, _ = generator_fixture
    payload = {"summary_250": "Summary text"}
    embedding_repo.async_get_summary_embedding.return_value = {
        "model_name": "test-model",
        "dimensions": 3,
        "content_hash": _expected_hash(payload),
    }

    created = await generator.generate_embedding_for_summary(
        10,
        payload,
        language="en",
    )

    assert created is False
    embedding_service.generate_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_embedding_for_summary_regenerates_when_content_hash_differs(
    generator_fixture,
) -> None:
    generator, embedding_service, embedding_repo, _, _ = generator_fixture
    embedding_repo.async_get_summary_embedding.return_value = {
        "model_name": "test-model",
        "dimensions": 3,
        "content_hash": "stale-hash",
    }

    created = await generator.generate_embedding_for_summary(
        10,
        {"summary_250": "Summary text"},
        language="en",
    )

    assert created is True
    embedding_service.generate_embedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_embedding_for_summary_regenerates_when_model_changes(
    generator_fixture,
) -> None:
    generator, embedding_service, embedding_repo, _, _ = generator_fixture
    embedding_repo.async_get_summary_embedding.return_value = {
        "model_name": "other-model",
        "dimensions": 768,
    }

    created = await generator.generate_embedding_for_summary(
        10,
        {"summary_250": "Summary text"},
        language="en",
    )

    assert created is True
    embedding_service.generate_embedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_embedding_for_summary_returns_false_for_empty_prepared_text(
    generator_fixture,
) -> None:
    generator, embedding_service, _, _, _ = generator_fixture

    with patch(
        "app.application.services.summary_embedding_generator.prepare_text_for_embedding",
        return_value="",
    ):
        created = await generator.generate_embedding_for_summary(10, {"summary_250": "ignored"})

    assert created is False
    embedding_service.generate_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_embedding_for_summary_persists_generated_vector(generator_fixture) -> None:
    generator, embedding_service, embedding_repo, _, _ = generator_fixture

    payload = {
        "summary_1000": "Detailed summary",
        "topic_tags": ["#ai"],
        "metadata": {"title": "Title"},
    }
    created = await generator.generate_embedding_for_summary(
        11,
        payload,
        language="ru",
        force=True,
    )

    assert created is True
    embedding_service.generate_embedding.assert_awaited_once()
    embedding_repo.async_create_or_update_summary_embedding.assert_awaited_once_with(
        summary_id=11,
        embedding_blob=b"serialized",
        model_name="test-model",
        model_version="2.0",
        dimensions=3,
        language="ru",
        content_hash=_expected_hash(payload),
    )


@pytest.mark.asyncio
async def test_generate_embedding_for_summary_handles_embedding_errors(generator_fixture) -> None:
    generator, embedding_service, _, _, _ = generator_fixture
    embedding_service.generate_embedding.side_effect = RuntimeError("boom")

    created = await generator.generate_embedding_for_summary(
        12,
        {"summary_250": "Summary text"},
    )

    assert created is False


@pytest.mark.asyncio
async def test_dependency_error_is_logged_without_traceback(generator_fixture) -> None:
    """A missing torch/CUDA backend must not spew a traceback per summary --
    callers degrade quietly with a single concise warning, no exc_info.

    The module logger is patched directly (rather than asserting via caplog)
    because the bot test suite reconfigures loguru's root handler, which makes
    caplog capture order-dependent.
    """
    generator, embedding_service, _, _, _ = generator_fixture
    embedding_service.generate_embedding.side_effect = EmbeddingDependencyUnavailableError(
        "libcudart.so.13: cannot open shared object file"
    )

    with patch("app.application.services.summary_embedding_generator.logger") as mock_logger:
        first = await generator.generate_embedding_for_summary(12, {"summary_250": "text"})
        second = await generator.generate_embedding_for_summary(13, {"summary_250": "text"})

    assert first is False
    assert second is False
    # No traceback dumped: logger.exception (which carries exc_info) is never
    # used for the dependency-unavailable path.
    mock_logger.exception.assert_not_called()
    # The dependency outage is surfaced at most once, not once per summary.
    assert mock_logger.warning.call_count == 1
    warning_event = mock_logger.warning.call_args[0][0]
    assert "dependency" in warning_event.lower()


class TestGenerateEmbeddingsForSummaries:
    @pytest.mark.asyncio
    async def test_batches_per_language_and_persists_each(self, generator_fixture) -> None:
        generator, embedding_service, embedding_repo, _, _ = generator_fixture
        embedding_service.get_model_name.side_effect = lambda lang=None: f"model-{lang}"
        embedding_service.generate_embeddings_batch = AsyncMock(
            side_effect=lambda texts, **_kw: [[0.0] for _ in texts]
        )

        items = [
            (1, {"summary_250": "a"}, "en"),
            (2, {"summary_250": "b"}, "en"),
            (3, {"summary_250": "c"}, "ru"),
        ]
        result = await generator.generate_embeddings_for_summaries(items, force=True)

        assert (result.indexed, result.skipped, result.failed) == (3, 0, 0)
        # One batched encode per language (2), not one per row (3).
        assert embedding_service.generate_embeddings_batch.await_count == 2
        assert embedding_repo.async_create_or_update_summary_embedding.await_count == 3

    @pytest.mark.asyncio
    async def test_skips_non_dict_and_empty_text(self, generator_fixture) -> None:
        generator, embedding_service, _, _, _ = generator_fixture
        embedding_service.generate_embeddings_batch = AsyncMock(return_value=[[0.1]])

        items = [
            (1, "legacy-string", None),  # non-dict -> skipped
            (2, {"summary_250": "ok"}, None),  # embeds
        ]
        result = await generator.generate_embeddings_for_summaries(items, force=True)

        assert result.skipped == 1
        assert result.indexed == 1
        # Only the embeddable row reaches the batch encode.
        texts = embedding_service.generate_embeddings_batch.await_args.args[0]
        assert len(texts) == 1

    @pytest.mark.asyncio
    async def test_skips_already_indexed_when_not_forced(self, generator_fixture) -> None:
        generator, embedding_service, embedding_repo, _, _ = generator_fixture
        payload = {"summary_250": "Summary text"}
        embedding_repo.async_get_summary_embedding.return_value = {
            "model_name": "test-model",
            "content_hash": _expected_hash(payload),
        }
        embedding_service.generate_embeddings_batch = AsyncMock(return_value=[])

        result = await generator.generate_embeddings_for_summaries(
            [(1, payload, "en")], force=False
        )

        assert result.skipped == 1
        assert result.indexed == 0
        embedding_service.generate_embeddings_batch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dependency_unavailable_skips_group_once(self, generator_fixture) -> None:
        generator, embedding_service, _, _, _ = generator_fixture
        embedding_service.generate_embeddings_batch = AsyncMock(
            side_effect=EmbeddingDependencyUnavailableError("no torch")
        )

        with patch("app.application.services.summary_embedding_generator.logger") as mock_logger:
            result = await generator.generate_embeddings_for_summaries(
                [(1, {"summary_250": "a"}, "en"), (2, {"summary_250": "b"}, "en")],
                force=True,
            )

        assert (result.indexed, result.skipped, result.failed) == (0, 2, 0)
        mock_logger.exception.assert_not_called()
        assert mock_logger.warning.call_count == 1

    @pytest.mark.asyncio
    async def test_batch_encode_failure_counts_as_failed(self, generator_fixture) -> None:
        generator, embedding_service, _, _, _ = generator_fixture
        embedding_service.generate_embeddings_batch = AsyncMock(side_effect=RuntimeError("boom"))

        result = await generator.generate_embeddings_for_summaries(
            [(1, {"summary_250": "a"}, "en")], force=True
        )

        assert result.failed == 1
        assert result.indexed == 0


@pytest.mark.asyncio
async def test_generate_embedding_for_request_handles_missing_request_summary_or_payload(
    generator_fixture,
) -> None:
    generator, _, _, request_repo, summary_repo = generator_fixture

    request_repo.async_get_request_by_id.return_value = None
    assert await generator.generate_embedding_for_request(100) is False

    request_repo.async_get_request_by_id.return_value = {"id": 100, "lang_detected": "en"}
    summary_repo.async_get_summary_by_request.return_value = None
    assert await generator.generate_embedding_for_request(100) is False

    summary_repo.async_get_summary_by_request.return_value = {"id": 5, "json_payload": None}
    assert await generator.generate_embedding_for_request(100) is False


@pytest.mark.asyncio
async def test_generate_embedding_for_request_delegates_to_summary_generation(
    generator_fixture,
) -> None:
    generator, _, _, request_repo, summary_repo = generator_fixture
    request_repo.async_get_request_by_id.return_value = {"id": 101, "lang_detected": "en"}
    summary_repo.async_get_summary_by_request.return_value = {
        "id": 9,
        "json_payload": {"summary_250": "Summary text"},
    }

    with patch.object(
        generator, "generate_embedding_for_summary", new=AsyncMock(return_value=True)
    ) as generate:
        created = await generator.generate_embedding_for_request(101, force=True)

    assert created is True
    generate.assert_awaited_once_with(
        summary_id=9,
        payload={"summary_250": "Summary text"},
        language="en",
        force=True,
    )
