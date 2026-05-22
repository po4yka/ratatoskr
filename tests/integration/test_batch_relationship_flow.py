"""Integration tests for batch relationship detection and combined summary flow."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.mark.integration
class TestBatchRelationshipFlow(unittest.IsolatedAsyncioTestCase):
    """Integration tests for the complete batch relationship flow."""

    async def asyncSetUp(self):
        """Set up test fixtures with a temporary database."""
        if not os.environ.get("TEST_DATABASE_URL"):
            self.skipTest("TEST_DATABASE_URL is required for Postgres-backed batch tests")

        from app.db.models import Request, Summary, User
        from app.db.session import DatabaseSessionManager

        # Create temporary database
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115
        self.db_path = self.tmp_db.name
        self.tmp_db.close()  # Close so DatabaseSessionManager can open it

        # Initialize database with session manager
        self.session_manager = DatabaseSessionManager(path=self.db_path)
        self.session_manager.migrate()

        # Create test user
        self.user = User.create(
            telegram_user_id=123456789,
            username="testuser",
            is_owner=True,
        )

        # Create test requests and summaries for a series
        self.series_requests = []
        self.series_summaries = []

        for i in range(1, 4):
            request = Request.create(
                type="url",
                status="ok",
                correlation_id=f"series-test-{i}",
                user_id=self.user.telegram_user_id,
                input_url=f"https://habr.com/ru/articles/{990000 + i}/",
                normalized_url=f"https://habr.com/ru/articles/{990000 + i}/",
                dedupe_hash=f"hash-series-{i}",
            )
            self.series_requests.append(request)

            summary_payload = {
                "title": f"Python Tutorial Part {i}",
                "summary_250": f"Part {i} of the Python tutorial series.",
                "summary_1000": f"Comprehensive part {i} covering Python concepts.",
                "key_ideas": [f"Idea {j} from part {i}" for j in range(1, 4)],
                "topic_tags": ["#python", "#tutorial", "#programming"],
                "entities": [{"name": "Python"}, {"name": "programming"}],
                "estimated_reading_time_min": 5 + i,
                "author": "John Doe",
            }

            summary = Summary.create(
                request=request,
                lang="en",
                json_payload=json.dumps(summary_payload),
            )
            self.series_summaries.append(summary)

        # Create test requests for unrelated articles
        self.unrelated_requests = []
        self.unrelated_summaries = []

        topics = [
            ("Italian Cooking", "#food", "Chef Mario"),
            ("Football Preview", "#sports", "Sports Writer"),
        ]

        for i, (title, tag, author) in enumerate(topics, 1):
            request = Request.create(
                type="url",
                status="ok",
                correlation_id=f"unrelated-test-{i}",
                user_id=self.user.telegram_user_id,
                input_url=f"https://example{i}.com/article",
                normalized_url=f"https://example{i}.com/article",
                dedupe_hash=f"hash-unrelated-{i}",
            )
            self.unrelated_requests.append(request)

            summary_payload = {
                "title": title,
                "summary_250": f"Summary about {title.lower()}.",
                "summary_1000": f"Detailed article about {title.lower()}.",
                "key_ideas": [f"Key point about {title.lower()}"],
                "topic_tags": [tag],
                "entities": [],
                "estimated_reading_time_min": 5,
                "author": author,
            }

            summary = Summary.create(
                request=request,
                lang="en",
                json_payload=json.dumps(summary_payload),
            )
            self.unrelated_summaries.append(summary)

    async def asyncTearDown(self):
        """Clean up temporary database."""
        # Close database connection via underlying peewee database
        if hasattr(self, "session_manager") and hasattr(self.session_manager, "_database"):
            try:
                self.session_manager._database.close()
            except Exception:
                pass

        # Clean up temp file
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    async def test_relationship_agent_detects_series(self):
        """Test that RelationshipAnalysisAgent correctly detects a series."""
        from app.adapter_models.batch_analysis import (
            ArticleMetadata,
            RelationshipAnalysisInput,
            RelationshipType,
        )
        from app.agents.relationship_analysis_agent import RelationshipAnalysisAgent

        # Build article metadata from test data
        articles = []
        for request, summary in zip(self.series_requests, self.series_summaries, strict=True):
            payload = json.loads(summary.json_payload)
            articles.append(
                ArticleMetadata(
                    request_id=request.id,
                    url=request.normalized_url,
                    title=payload.get("title"),
                    author=payload.get("author"),
                    domain="habr.com",
                    topic_tags=payload.get("topic_tags", []),
                    entities=[e["name"] for e in payload.get("entities", [])],
                    summary_250=payload.get("summary_250"),
                )
            )

        agent = RelationshipAnalysisAgent(
            llm_client=None,  # No LLM needed for clear series
            correlation_id="test-series-detection",
        )

        input_data = RelationshipAnalysisInput(
            articles=articles,
            correlation_id="test-series-detection",
            series_threshold=0.8,
        )

        result = await agent.execute(input_data)

        self.assertTrue(result.success)
        self.assertEqual(result.output.relationship_type, RelationshipType.SERIES)
        self.assertGreaterEqual(result.output.confidence, 0.8)
        self.assertIsNotNone(result.output.series_info)
        self.assertEqual(result.output.series_info.numbering_pattern, "Part N")

    async def test_relationship_agent_detects_unrelated(self):
        """Test that RelationshipAnalysisAgent correctly identifies unrelated articles."""
        from app.adapter_models.batch_analysis import (
            ArticleMetadata,
            RelationshipAnalysisInput,
            RelationshipType,
        )
        from app.agents.relationship_analysis_agent import RelationshipAnalysisAgent

        # Build article metadata from test data
        articles = []
        for request, summary in zip(self.unrelated_requests, self.unrelated_summaries, strict=True):
            payload = json.loads(summary.json_payload)
            articles.append(
                ArticleMetadata(
                    request_id=request.id,
                    url=request.normalized_url,
                    title=payload.get("title"),
                    author=payload.get("author"),
                    domain=request.normalized_url.split("/")[2],
                    topic_tags=payload.get("topic_tags", []),
                    entities=[],
                    summary_250=payload.get("summary_250"),
                )
            )

        agent = RelationshipAnalysisAgent(
            llm_client=None,
            correlation_id="test-unrelated-detection",
        )

        input_data = RelationshipAnalysisInput(
            articles=articles,
            correlation_id="test-unrelated-detection",
        )

        result = await agent.execute(input_data)

        self.assertTrue(result.success)
        self.assertEqual(result.output.relationship_type, RelationshipType.UNRELATED)

    async def test_combined_summary_agent_generates_synthesis(self):
        """Test that CombinedSummaryAgent generates a combined summary."""
        from app.adapter_models.batch_analysis import (
            ArticleMetadata,
            CombinedSummaryInput,
            RelationshipAnalysisOutput,
            RelationshipType,
            SeriesInfo,
        )
        from app.agents.combined_summary_agent import CombinedSummaryAgent

        # Build article metadata
        articles = []
        full_summaries = []

        for request, summary in zip(self.series_requests, self.series_summaries, strict=True):
            payload = json.loads(summary.json_payload)
            articles.append(
                ArticleMetadata(
                    request_id=request.id,
                    url=request.normalized_url,
                    title=payload.get("title"),
                    author=payload.get("author"),
                    domain="habr.com",
                    topic_tags=payload.get("topic_tags", []),
                    entities=[e["name"] for e in payload.get("entities", [])],
                    summary_250=payload.get("summary_250"),
                    summary_1000=payload.get("summary_1000"),
                    language="en",
                )
            )
            full_summaries.append(payload)

        # Create relationship output
        relationship = RelationshipAnalysisOutput(
            relationship_type=RelationshipType.SERIES,
            confidence=0.95,
            series_info=SeriesInfo(
                series_title="Python Tutorial",
                article_order=[r.id for r in self.series_requests],
                numbering_pattern="Part N",
                confidence=0.95,
            ),
            reasoning="Detected Part N numbering pattern",
            signals_used=["series_patterns"],
        )

        # Mock LLM client
        mock_llm = MagicMock()
        mock_result = MagicMock()
        mock_result.parsed.model_dump.return_value = {
            "thematic_arc": "A comprehensive Python tutorial series covering basics to advanced topics.",
            "synthesized_insights": [
                "Python fundamentals build progressively",
                "Each part builds on previous concepts",
            ],
            "contradictions": [],
            "complementary_points": ["Parts complement each other well"],
            "recommended_reading_order": [r.id for r in self.series_requests],
            "reading_order_rationale": "Follow the natural Part 1, 2, 3 progression.",
            "combined_key_ideas": ["Python basics", "Progressive learning"],
            "combined_entities": ["Python"],
            "combined_topic_tags": ["#python", "#tutorial"],
            "total_reading_time_min": sum(5 + i for i in range(1, 4)),
        }
        mock_llm.chat_structured = AsyncMock(return_value=mock_result)

        agent = CombinedSummaryAgent(
            llm_client=mock_llm,
            correlation_id="test-combined-summary",
        )

        input_data = CombinedSummaryInput(
            articles=articles,
            relationship=relationship,
            full_summaries=full_summaries,
            correlation_id="test-combined-summary",
            language="en",
        )

        result = await agent.execute(input_data)

        self.assertTrue(result.success)
        self.assertIsNotNone(result.output)
        self.assertIn("Python", result.output.thematic_arc)
        self.assertTrue(len(result.output.synthesized_insights) >= 1)
        self.assertEqual(
            result.output.recommended_reading_order,
            [r.id for r in self.series_requests],
        )

    async def test_batch_session_repository_operations(self):
        """Test BatchSessionRepository CRUD operations."""
        from app.infrastructure.persistence.repositories.batch_session_repository import (
            BatchSessionRepositoryAdapter,
        )

        # Use the session manager from setup
        repo = BatchSessionRepositoryAdapter(self.session_manager)

        # Test create
        session_id = await repo.async_create_batch_session(
            user_id=self.user.telegram_user_id,
            correlation_id="test-batch-session-123",
            total_urls=3,
        )
        self.assertIsNotNone(session_id)
        self.assertGreater(session_id, 0)

        # Test get
        session = await repo.async_get_batch_session(session_id)
        self.assertIsNotNone(session)
        self.assertEqual(session["total_urls"], 3)
        self.assertEqual(session["status"], "processing")

        # Test add items
        for i, request in enumerate(self.series_requests):
            await repo.async_add_batch_session_item(
                session_id=session_id,
                request_id=request.id,
                position=i,
            )

        # Test get items
        items = await repo.async_get_batch_session_items(session_id)
        self.assertEqual(len(items), 3)

        # Test update counts
        await repo.async_update_batch_session_counts(session_id, successful_count=3, failed_count=0)

        # Test update relationship
        await repo.async_update_batch_session_relationship(
            session_id,
            relationship_type="series",
            relationship_confidence=0.95,
            relationship_metadata={"pattern": "Part N"},
        )

        # Verify updates
        updated_session = await repo.async_get_batch_session(session_id)
        self.assertEqual(updated_session["successful_count"], 3)
        self.assertEqual(updated_session["relationship_type"], "series")
        self.assertAlmostEqual(updated_session["relationship_confidence"], 0.95, places=2)

        # Test get with summaries
        session_with_summaries = await repo.async_get_batch_session_with_summaries(session_id)
        self.assertIsNotNone(session_with_summaries)
        self.assertEqual(len(session_with_summaries["items"]), 3)

        # Test update status
        await repo.async_update_batch_session_status(
            session_id, "completed", analysis_status="complete", processing_time_ms=1500
        )

        final_session = await repo.async_get_batch_session(session_id)
        self.assertEqual(final_session["status"], "completed")
        self.assertEqual(final_session["analysis_status"], "complete")
        self.assertEqual(final_session["processing_time_ms"], 1500)

    async def test_batch_analysis_config_loaded(self):
        """Test that BatchAnalysisConfig is properly loaded."""
        from app.config.integrations import BatchAnalysisConfig

        # Test default values
        config = BatchAnalysisConfig()
        self.assertTrue(config.enabled)
        self.assertEqual(config.min_articles, 2)
        self.assertAlmostEqual(config.series_threshold, 0.9, places=2)
        self.assertAlmostEqual(config.cluster_threshold, 0.75, places=2)
        self.assertTrue(config.combined_summary_enabled)
        self.assertTrue(config.use_llm_for_analysis)

    async def test_end_to_end_series_detection_flow(self):
        """Test the complete flow from articles to combined summary."""
        import json

        from app.adapter_models.batch_analysis import (
            ArticleMetadata,
            CombinedSummaryInput,
            RelationshipAnalysisInput,
            RelationshipType,
        )
        from app.agents.combined_summary_agent import CombinedSummaryAgent
        from app.agents.relationship_analysis_agent import RelationshipAnalysisAgent

        # Step 1: Build article metadata
        articles = []
        full_summaries = []

        for request, summary in zip(self.series_requests, self.series_summaries, strict=True):
            payload = json.loads(summary.json_payload)
            articles.append(
                ArticleMetadata(
                    request_id=request.id,
                    url=request.normalized_url,
                    title=payload.get("title"),
                    author=payload.get("author"),
                    domain="habr.com",
                    topic_tags=payload.get("topic_tags", []),
                    entities=[e["name"] for e in payload.get("entities", [])],
                    summary_250=payload.get("summary_250"),
                    summary_1000=payload.get("summary_1000"),
                    language="en",
                )
            )
            full_summaries.append(payload)

        # Step 2: Run relationship analysis
        rel_agent = RelationshipAnalysisAgent(
            llm_client=None,
            correlation_id="e2e-test",
        )

        rel_input = RelationshipAnalysisInput(
            articles=articles,
            correlation_id="e2e-test",
            series_threshold=0.8,
        )

        rel_result = await rel_agent.execute(rel_input)

        self.assertTrue(rel_result.success)
        self.assertEqual(rel_result.output.relationship_type, RelationshipType.SERIES)

        # Step 3: If series detected, generate combined summary
        if rel_result.output.relationship_type != RelationshipType.UNRELATED:
            # Mock LLM for combined summary
            mock_llm = MagicMock()
            mock_result = MagicMock()
            mock_result.parsed.model_dump.return_value = {
                "thematic_arc": "Complete Python tutorial journey.",
                "synthesized_insights": ["Progressive learning works"],
                "contradictions": [],
                "complementary_points": ["Each part complements others"],
                "recommended_reading_order": [r.id for r in self.series_requests],
                "reading_order_rationale": "Sequential order.",
                "combined_key_ideas": ["Python mastery"],
                "combined_entities": ["Python"],
                "combined_topic_tags": ["#python"],
                "total_reading_time_min": 18,
            }
            mock_llm.chat_structured = AsyncMock(return_value=mock_result)

            combined_agent = CombinedSummaryAgent(
                llm_client=mock_llm,
                correlation_id="e2e-test",
            )

            combined_input = CombinedSummaryInput(
                articles=articles,
                relationship=rel_result.output,
                full_summaries=full_summaries,
                correlation_id="e2e-test",
                language="en",
            )

            combined_result = await combined_agent.execute(combined_input)

            self.assertTrue(combined_result.success)
            self.assertIsNotNone(combined_result.output.thematic_arc)


if __name__ == "__main__":
    unittest.main()
