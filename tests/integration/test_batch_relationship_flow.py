"""Integration tests for the current batch relationship and synthesis flow.

Batch-session repository CRUD is covered by the SQLAlchemy-backed
``tests/infrastructure/test_batch_session_repository_postgres.py`` suite. These
tests exercise the agent flow itself with realistic persisted-summary shapes and
mocked LLM output, so they require no legacy SQLite/Peewee session manager.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapter_models.batch_analysis import (
    ArticleMetadata,
    CombinedSummaryInput,
    RelationshipAnalysisInput,
    RelationshipAnalysisOutput,
    RelationshipType,
    SeriesInfo,
)
from app.agents.combined_summary_agent import CombinedSummaryAgent
from app.agents.relationship_analysis_agent import RelationshipAnalysisAgent
from app.config.integrations import BatchAnalysisConfig

pytestmark = pytest.mark.integration


class TestBatchRelationshipFlow(unittest.IsolatedAsyncioTestCase):
    """Exercise metadata relationship detection and synthesized batch output."""

    def setUp(self) -> None:
        self.series_articles = [
            ArticleMetadata(
                request_id=index,
                url=f"https://habr.com/ru/articles/{990000 + index}/",
                title=f"Python Tutorial Part {index}",
                author="John Doe",
                domain="habr.com",
                topic_tags=["#python", "#tutorial", "#programming"],
                entities=["Python", "programming"],
                summary_250=f"Part {index} of the Python tutorial series.",
                summary_1000=f"Comprehensive part {index} covering Python concepts.",
                language="en",
            )
            for index in range(1, 4)
        ]
        self.series_summaries = [
            {
                "title": article.title,
                "summary_250": article.summary_250,
                "summary_1000": article.summary_1000,
                "key_ideas": [f"Idea {item} from part {index}" for item in range(1, 4)],
                "topic_tags": article.topic_tags,
                "entities": [{"name": entity} for entity in article.entities],
                "estimated_reading_time_min": 5 + index,
                "author": article.author,
            }
            for index, article in enumerate(self.series_articles, 1)
        ]
        self.unrelated_articles = [
            ArticleMetadata(
                request_id=10,
                url="https://cooking.example/recipes",
                title="Italian Cooking",
                author="Chef Mario",
                domain="cooking.example",
                topic_tags=["#food"],
                entities=[],
                summary_250="Summary about Italian cooking.",
            ),
            ArticleMetadata(
                request_id=11,
                url="https://sports.example/preview",
                title="Football Preview",
                author="Sports Writer",
                domain="sports.example",
                topic_tags=["#sports"],
                entities=[],
                summary_250="Summary about football.",
            ),
        ]

    async def test_relationship_agent_detects_series(self) -> None:
        agent = RelationshipAnalysisAgent(llm_client=None, correlation_id="test-series-detection")

        result = await agent.execute(
            RelationshipAnalysisInput(
                articles=self.series_articles,
                correlation_id="test-series-detection",
                series_threshold=0.8,
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(result.output.relationship_type, RelationshipType.SERIES)
        self.assertGreaterEqual(result.output.confidence, 0.8)
        self.assertEqual(result.output.series_info.numbering_pattern, "Part N")

    async def test_relationship_agent_detects_unrelated_articles(self) -> None:
        agent = RelationshipAnalysisAgent(
            llm_client=None, correlation_id="test-unrelated-detection"
        )

        result = await agent.execute(
            RelationshipAnalysisInput(
                articles=self.unrelated_articles,
                correlation_id="test-unrelated-detection",
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(result.output.relationship_type, RelationshipType.UNRELATED)

    async def test_combined_summary_agent_generates_synthesis(self) -> None:
        mock_llm = MagicMock()
        mock_result = MagicMock()
        mock_result.parsed.model_dump.return_value = _combined_output(self.series_articles)
        mock_llm.chat_structured = AsyncMock(return_value=mock_result)
        agent = CombinedSummaryAgent(llm_client=mock_llm, correlation_id="test-combined-summary")

        result = await agent.execute(
            CombinedSummaryInput(
                articles=self.series_articles,
                relationship=_series_relationship(self.series_articles),
                full_summaries=self.series_summaries,
                correlation_id="test-combined-summary",
                language="en",
            )
        )

        self.assertTrue(result.success)
        self.assertIn("Python", result.output.thematic_arc)
        self.assertEqual(
            result.output.recommended_reading_order,
            [article.request_id for article in self.series_articles],
        )

    def test_batch_analysis_config_defaults_are_loaded(self) -> None:
        config = BatchAnalysisConfig()

        self.assertTrue(config.enabled)
        self.assertEqual(config.min_articles, 2)
        self.assertAlmostEqual(config.series_threshold, 0.9, places=2)
        self.assertTrue(config.combined_summary_enabled)
        self.assertTrue(config.use_llm_for_analysis)

    async def test_end_to_end_series_detection_and_synthesis(self) -> None:
        relationship_agent = RelationshipAnalysisAgent(llm_client=None, correlation_id="e2e-test")
        relationship_result = await relationship_agent.execute(
            RelationshipAnalysisInput(
                articles=self.series_articles,
                correlation_id="e2e-test",
                series_threshold=0.8,
            )
        )
        self.assertTrue(relationship_result.success)
        self.assertEqual(relationship_result.output.relationship_type, RelationshipType.SERIES)

        mock_llm = MagicMock()
        mock_result = MagicMock()
        mock_result.parsed.model_dump.return_value = _combined_output(self.series_articles)
        mock_llm.chat_structured = AsyncMock(return_value=mock_result)
        combined_agent = CombinedSummaryAgent(llm_client=mock_llm, correlation_id="e2e-test")
        combined_result = await combined_agent.execute(
            CombinedSummaryInput(
                articles=self.series_articles,
                relationship=relationship_result.output,
                full_summaries=self.series_summaries,
                correlation_id="e2e-test",
                language="en",
            )
        )

        self.assertTrue(combined_result.success)
        self.assertIn("Python", combined_result.output.thematic_arc)


def _series_relationship(articles: list[ArticleMetadata]) -> RelationshipAnalysisOutput:
    return RelationshipAnalysisOutput(
        relationship_type=RelationshipType.SERIES,
        confidence=0.95,
        series_info=SeriesInfo(
            series_title="Python Tutorial",
            article_order=[article.request_id for article in articles],
            numbering_pattern="Part N",
            confidence=0.95,
        ),
        reasoning="Detected Part N numbering pattern",
        signals_used=["series_patterns"],
    )


def _combined_output(articles: list[ArticleMetadata]) -> dict[str, object]:
    return {
        "thematic_arc": "A comprehensive Python tutorial series covering basics to advanced topics.",
        "synthesized_insights": [
            "Python fundamentals build progressively",
            "Each part builds on previous concepts",
        ],
        "contradictions": [],
        "complementary_points": ["Parts complement each other well"],
        "recommended_reading_order": [article.request_id for article in articles],
        "reading_order_rationale": "Follow the natural Part 1, 2, 3 progression.",
        "combined_key_ideas": ["Python basics", "Progressive learning"],
        "combined_entities": ["Python"],
        "combined_topic_tags": ["#python", "#tutorial"],
        "total_reading_time_min": 21,
    }
