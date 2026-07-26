"""Unit tests for CombinedSummaryAgent."""

import unittest
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

from app.adapter_models.batch_analysis import (
    ArticleMetadata,
    ClusterInfo,
    CombinedSummaryInput,
    RelationshipAnalysisOutput,
    RelationshipType,
    SeriesInfo,
)
from app.agents.combined_summary_agent import CombinedSummaryAgent, _CombinedSummaryLLMResponse


class TestCombinedSummaryAgent(unittest.IsolatedAsyncioTestCase):
    """Test CombinedSummaryAgent functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.correlation_id = "combined-summary-test-123"
        self.mock_llm = MagicMock()

        self.agent = CombinedSummaryAgent(
            llm_client=self.mock_llm,
            correlation_id=self.correlation_id,
        )

        # Sample articles
        self.articles = [
            ArticleMetadata(
                request_id=1,
                url="https://example.com/article1",
                title="Python Tutorial Part 1",
                author="John Doe",
                domain="example.com",
                topic_tags=["#python", "#tutorial"],
                entities=["Python"],
                summary_250="Introduction to Python basics.",
                summary_1000="Comprehensive introduction to Python programming.",
            ),
            ArticleMetadata(
                request_id=2,
                url="https://example.com/article2",
                title="Python Tutorial Part 2",
                author="John Doe",
                domain="example.com",
                topic_tags=["#python", "#tutorial"],
                entities=["Python", "functions"],
                summary_250="Learn about Python functions.",
                summary_1000="Deep dive into Python functions and modules.",
            ),
        ]

        self.full_summaries = [
            {
                "title": "Python Tutorial Part 1",
                "summary_250": "Introduction to Python basics.",
                "summary_1000": "Comprehensive introduction to Python programming.",
                "key_ideas": ["Python is versatile", "Easy to learn"],
                "topic_tags": ["#python", "#tutorial"],
                "entities": [{"name": "Python"}],
                "estimated_reading_time_min": 5,
            },
            {
                "title": "Python Tutorial Part 2",
                "summary_250": "Learn about Python functions.",
                "summary_1000": "Deep dive into Python functions and modules.",
                "key_ideas": ["Functions are reusable", "Modules organize code"],
                "topic_tags": ["#python", "#tutorial"],
                "entities": [{"name": "Python"}, {"name": "functions"}],
                "estimated_reading_time_min": 7,
            },
        ]

        self.series_relationship = RelationshipAnalysisOutput(
            relationship_type=RelationshipType.SERIES,
            confidence=0.95,
            series_info=SeriesInfo(
                series_title="Python Tutorial",
                article_order=[1, 2],
                numbering_pattern="Part N",
                confidence=0.95,
            ),
            reasoning="Detected sequential Part N numbering",
            signals_used=["series_patterns", "title_numbering"],
        )

        self.cluster_relationship = RelationshipAnalysisOutput(
            relationship_type=RelationshipType.TOPIC_CLUSTER,
            confidence=0.85,
            cluster_info=ClusterInfo(
                cluster_topic="Python Programming",
                shared_entities=["Python"],
                shared_tags=["#python"],
                perspectives=["Basics", "Functions"],
                confidence=0.85,
            ),
            reasoning="Strong topic overlap in Python programming",
            signals_used=["entity_overlap", "tag_overlap"],
        )

        # Mock LLM response
        self.valid_llm_response = """{
            "thematic_arc": "A comprehensive journey through Python programming, from basics to functions.",
            "synthesized_insights": [
                "Python's simplicity makes it ideal for beginners",
                "Functions are essential building blocks for clean code",
                "Progressive learning builds on fundamental concepts"
            ],
            "contradictions": [],
            "complementary_points": [
                "Article 1 provides foundation, Article 2 builds on it"
            ],
            "recommended_reading_order": [1, 2],
            "reading_order_rationale": "Read in sequence as Part 1 establishes basics needed for Part 2.",
            "combined_key_ideas": [
                "Python is versatile",
                "Functions enable code reuse",
                "Clean code matters"
            ],
            "combined_entities": ["Python", "functions"],
            "combined_topic_tags": ["#python", "#tutorial", "#programming"],
            "total_reading_time_min": 12
        }"""

    def test_structured_response_rejects_empty_payload(self):
        with self.assertRaises(ValidationError):
            _CombinedSummaryLLMResponse.model_validate({})

    def test_structured_response_rejects_untyped_collection_items(self):
        with self.assertRaises(ValidationError):
            _CombinedSummaryLLMResponse.model_validate(
                {
                    "thematic_arc": "Shared arc",
                    "synthesized_insights": [{"unexpected": "object"}],
                    "recommended_reading_order": [1, 2],
                }
            )

    def _make_structured_result(self, parsed_dict: dict) -> MagicMock:
        """Build a StructuredLLMResult-compatible mock for chat_structured."""
        structured = MagicMock()
        structured.parsed.model_dump.return_value = parsed_dict
        structured.cost_usd = 0.0
        return structured

    async def test_successful_combined_summary_for_series(self):
        """Test successful combined summary generation for series."""
        import json

        self.mock_llm.chat_structured = AsyncMock(
            return_value=self._make_structured_result(json.loads(self.valid_llm_response))
        )

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=self.series_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="en",
        )

        result = await self.agent.execute(input_data)

        self.assertTrue(result.success)
        self.assertIsNotNone(result.output)
        self.assertIn("Python", result.output.thematic_arc)
        self.assertTrue(len(result.output.synthesized_insights) > 0)
        self.assertEqual(result.output.recommended_reading_order, [1, 2])
        self.assertEqual(result.output.total_reading_time_min, 12)

    async def test_successful_combined_summary_for_cluster(self):
        """Test successful combined summary generation for topic cluster."""
        import json

        self.mock_llm.chat_structured = AsyncMock(
            return_value=self._make_structured_result(json.loads(self.valid_llm_response))
        )

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=self.cluster_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="en",
        )

        result = await self.agent.execute(input_data)

        self.assertTrue(result.success)
        self.assertIsNotNone(result.output)

    async def test_error_for_single_article(self):
        """Test error when only one article provided."""
        input_data = CombinedSummaryInput(
            articles=[self.articles[0]],
            relationship=self.series_relationship,
            full_summaries=[self.full_summaries[0]],
            correlation_id=self.correlation_id,
            language="en",
        )

        result = await self.agent.execute(input_data)

        self.assertFalse(result.success)
        self.assertIn("at least 2 articles", result.error)

    async def test_error_for_unrelated_articles(self):
        """Test error when articles are unrelated."""
        unrelated_relationship = RelationshipAnalysisOutput(
            relationship_type=RelationshipType.UNRELATED,
            confidence=0.9,
            reasoning="No meaningful connection",
            signals_used=["no_signals"],
        )

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=unrelated_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="en",
        )

        result = await self.agent.execute(input_data)

        self.assertFalse(result.success)
        self.assertIn("unrelated", result.error.lower())

    async def test_llm_failure_handled(self):
        """Test graceful handling of LLM failure."""
        self.mock_llm.chat_structured = AsyncMock(
            side_effect=RuntimeError("LLM service unavailable")
        )

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=self.series_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="en",
        )

        result = await self.agent.execute(input_data)

        self.assertFalse(result.success)

    async def test_invalid_json_response_handled(self):
        """Test handling of invalid JSON from LLM (Instructor raises on parse failure)."""
        self.mock_llm.chat_structured = AsyncMock(
            side_effect=ValueError("Failed to parse LLM response as valid JSON")
        )

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=self.series_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="en",
        )

        result = await self.agent.execute(input_data)

        self.assertFalse(result.success)

    async def test_reading_order_defaults_to_input_order(self):
        """Test that reading order defaults to input order if not provided."""
        response_no_order = """{
            "thematic_arc": "A Python journey.",
            "synthesized_insights": ["Python is great"],
            "contradictions": [],
            "complementary_points": [],
            "reading_order_rationale": "Sequential reading",
            "combined_key_ideas": ["Python"],
            "combined_entities": ["Python"],
            "combined_topic_tags": ["#python"],
            "total_reading_time_min": 10
        }"""

        import json

        self.mock_llm.chat_structured = AsyncMock(
            return_value=self._make_structured_result(json.loads(response_no_order))
        )

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=self.series_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="en",
        )

        result = await self.agent.execute(input_data)

        self.assertTrue(result.success)
        # Should default to input order [1, 2]
        self.assertEqual(result.output.recommended_reading_order, [1, 2])

    async def test_llm_context_includes_relationship_info(self):
        """Test that LLM context includes relationship information."""
        import json

        self.mock_llm.chat_structured = AsyncMock(
            return_value=self._make_structured_result(json.loads(self.valid_llm_response))
        )

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=self.series_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="en",
        )

        await self.agent.execute(input_data)

        # Verify LLM was called with context including relationship info
        self.mock_llm.chat_structured.assert_called_once()
        call_args = self.mock_llm.chat_structured.call_args
        messages = call_args[0][0]

        # User message should contain relationship type
        user_message = messages[1]["content"]
        self.assertIn("series", user_message.lower())
        self.assertIn("Part N", user_message)

    async def test_total_reading_time_calculated_from_summaries(self):
        """Test that total reading time is calculated from summaries if not in LLM response."""
        response_no_time = """{
            "thematic_arc": "A Python journey.",
            "synthesized_insights": ["Python is great"],
            "contradictions": [],
            "complementary_points": [],
            "recommended_reading_order": [1, 2],
            "reading_order_rationale": "Sequential reading",
            "combined_key_ideas": ["Python"],
            "combined_entities": ["Python"],
            "combined_topic_tags": ["#python"]
        }"""

        import json

        self.mock_llm.chat_structured = AsyncMock(
            return_value=self._make_structured_result(json.loads(response_no_time))
        )

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=self.series_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="en",
        )

        result = await self.agent.execute(input_data)

        self.assertTrue(result.success)
        # Should be sum of reading times: 5 + 7 = 12
        self.assertEqual(result.output.total_reading_time_min, 12)

    async def test_russian_language_prompt(self):
        """Test that Russian language uses correct prompt."""
        import json

        self.mock_llm.chat_structured = AsyncMock(
            return_value=self._make_structured_result(json.loads(self.valid_llm_response))
        )

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=self.series_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="ru",
        )

        await self.agent.execute(input_data)

        # Verify LLM was called (prompt loading is internal)
        self.mock_llm.chat_structured.assert_called_once()

    async def test_llm_exception_handled(self):
        """Test handling of LLM exception."""
        self.mock_llm.chat_structured = AsyncMock(side_effect=RuntimeError("Connection error"))

        input_data = CombinedSummaryInput(
            articles=self.articles,
            relationship=self.series_relationship,
            full_summaries=self.full_summaries,
            correlation_id=self.correlation_id,
            language="en",
        )

        result = await self.agent.execute(input_data)

        self.assertFalse(result.success)
        self.assertIn("generate combined summary", result.error)

    async def test_ensure_list_helper(self):
        """Test the _ensure_list helper method."""
        self.assertEqual(self.agent._ensure_list(["a", "b"]), ["a", "b"])
        self.assertEqual(self.agent._ensure_list("not a list"), [])
        self.assertEqual(self.agent._ensure_list(None), [])
        self.assertEqual(self.agent._ensure_list([1, 2, 3]), ["1", "2", "3"])
        self.assertEqual(self.agent._ensure_list(["a", "", None, "b"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
