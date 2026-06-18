import logging
import unittest

from app.core.summary_contract import (
    get_summary_contract_descriptor,
    get_summary_json_schema,
    validate_and_shape_summary,
)
from app.prompts.manager import PromptManager


def _minimal_summary_payload(**overrides):
    payload = {
        "summary_250": "Short summary.",
        "summary_1000": "Longer summary with enough detail.",
        "tldr": "Longer summary with enough detail and context.",
        "key_ideas": ["idea"],
    }
    payload.update(overrides)
    return payload


class TestSummaryContract(unittest.TestCase):
    def test_caps_and_tags_and_entities(self):
        payload = {
            "summary_250": "A" * 400 + " end.",
            "summary_1000": "B" * 1200 + " end.",
            "tldr": "C" * 1400 + " end.",
            "key_ideas": [" idea1 ", "", "idea2"],
            "topic_tags": ["tag1", "#Tag1", "tag2"],
            "entities": {
                "people": ["Alice", "alice", "Bob"],
                "organizations": ["ACME", "acme"],
                "locations": ["NY", "ny"],
            },
            "estimated_reading_time_min": "7",
        }

        out = validate_and_shape_summary(payload)

        assert len(out["summary_250"]) <= 250
        assert len(out["summary_1000"]) <= 1000
        assert out["tldr"].startswith("C" * 100)
        assert out["topic_tags"] == ["#tag1", "#tag2"]  # dedup + hash prefix
        assert out["entities"]["people"] == ["Alice", "Bob"]  # dedup case-insensitive
        assert out["estimated_reading_time_min"] == 7
        assert "key_stats" in out
        assert "readability" in out

    def test_entities_handles_list_payloads(self):
        payload = {
            "summary_250": "Short summary.",
            "summary_1000": "Longer summary that provides more detail.",
            "entities": [
                {
                    "type": "people",
                    "entities": ["Alice", {"name": "Bob"}],
                },
                {
                    "type": "organization",
                    "names": ["OpenAI", "Anthropic"],
                },
                {
                    "label": "locations",
                    "values": ["San Francisco", "New York"],
                },
                "Charlie",
            ],
        }

        out = validate_and_shape_summary(payload)

        assert out["summary_1000"] == "Longer summary that provides more detail."
        assert out["tldr"] == "Longer summary that provides more detail."
        assert out["entities"]["people"] == ["Alice", "Bob", "Charlie"]
        assert out["entities"]["organizations"] == ["OpenAI", "Anthropic"]
        assert out["entities"]["locations"] == ["San Francisco", "New York"]

    def test_fallback_summary_from_supporting_fields(self):
        payload = {
            "key_ideas": [
                "Key idea one highlights the main vulnerability.",
                "Key idea two explains the mitigation steps.",
            ],
            "highlights": ["Highlight content adds additional context."],
            "insights": {"topic_overview": "Overall, the article explores safety bypasses."},
        }

        out = validate_and_shape_summary(payload)

        assert out["summary_250"].strip()
        assert out["summary_1000"].strip()
        assert out["tldr"].strip()
        assert "Key idea one" in out["summary_1000"]

    def test_tldr_enriched_when_matching_summary(self):
        payload = {
            "summary_250": "Stripe animation trick.",
            "summary_1000": "Stripe animation trick using gradients and helper functions.",
            "tldr": "Stripe animation trick using gradients and helper functions.",
            "key_ideas": [
                "Color stops create sharp edges",
                "Helper functions simplify stripe spacing",
            ],
            "highlights": ["Avoids jagged edges and keeps motion smooth."],
            "key_stats": [{"label": "frames", "value": 120, "unit": "fps", "source_excerpt": None}],
            "answered_questions": [
                {"question": "How to keep stripes crisp?", "answer": "Use strategic color stops."}
            ],
        }

        out = validate_and_shape_summary(payload)

        assert out["tldr"] != out["summary_1000"]
        assert "Key ideas:" in out["tldr"]
        assert len(out["tldr"]) > len(out["summary_1000"])

    def test_key_stats_preserve_nested_camel_case_source_excerpt(self):
        payload = {
            "summary_250": "A short backend contract summary.",
            "summary_1000": "A longer backend contract summary.",
            "tldr": "Backend summary detail keeps key stats stable.",
            "key_ideas": ["quality metadata is public-safe"],
            "key_stats": [
                {
                    "label": "Contracts",
                    "value": "3",
                    "unit": None,
                    "sourceExcerpt": "Three contract checks passed.",
                }
            ],
        }

        out = validate_and_shape_summary(payload)

        assert out["key_stats"] == [
            {
                "label": "Contracts",
                "value": 3.0,
                "unit": None,
                "source_excerpt": "Three contract checks passed.",
            }
        ]

    def test_tldr_enriched_when_high_similarity(self):
        payload = {
            "summary_250": "Short hook.",
            "summary_1000": "Detailed paragraph about gradients, motion, and easing for stripes.",
            "tldr": "Detailed paragraph about gradients, motion, and easing.",
            "key_ideas": ["Easing reduces jitter", "Gradients control contrast"],
            "highlights": ["Adds easing to keep motion smooth."],
        }

        out = validate_and_shape_summary(payload)

        assert len(out["tldr"]) > len(payload["tldr"])
        assert "Key ideas:" in out["tldr"]

    def test_rag_fields_are_populated_and_capped(self):
        payload = {
            "summary_250": "Concise summary about renewable energy transition.",
            "summary_1000": (
                "The article explores the transition to renewable energy, highlighting policy incentives, "
                "grid modernization, storage breakthroughs, and regional cooperation. It notes trade-offs "
                "between reliability, cost, and speed of rollout."
            ),
            "tldr": "A fuller TLDR about renewable energy grids and storage economics.",
            "topic_tags": ["energy", "policy", "grid"],
            "seo_keywords": ["renewable energy", "grid modernization", "energy storage"],
            "key_ideas": ["Policy incentives", "Grid modernization", "Storage breakthroughs"],
        }

        out = validate_and_shape_summary(payload)

        assert "article_id" in out
        assert isinstance(out["query_expansion_keywords"], list)
        assert 1 <= len(out["semantic_boosters"]) <= 15
        assert len(out["query_expansion_keywords"]) <= 30
        assert "semantic_chunks" in out
        assert isinstance(out["semantic_chunks"], list)


if __name__ == "__main__":
    unittest.main()


def test_invalid_confidence_string_defaults_to_conservative_value(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.summary_contract_impl.summary_shaper"):
        out = validate_and_shape_summary(_minimal_summary_payload(confidence="very sure"))

    assert out["confidence"] == 0.0
    assert out["hallucination_risk"] == "unknown"
    assert "summary_confidence_invalid" in caplog.messages
    assert "summary_hallucination_risk_missing" in caplog.messages


def test_missing_confidence_defaults_to_conservative_value(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.summary_contract_impl.summary_shaper"):
        out = validate_and_shape_summary(_minimal_summary_payload(hallucination_risk="high"))

    assert out["confidence"] == 0.0
    assert out["hallucination_risk"] == "high"
    assert "summary_confidence_missing" in caplog.messages


def test_invalid_hallucination_risk_defaults_to_unknown(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.summary_contract_impl.summary_shaper"):
        out = validate_and_shape_summary(
            _minimal_summary_payload(confidence=0.8, hallucination_risk="definitely no risk")
        )

    assert out["confidence"] == 0.8
    assert out["hallucination_risk"] == "unknown"
    assert "summary_hallucination_risk_invalid" in caplog.messages


def test_invalid_source_type_and_freshness_warn_and_become_unknown(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.summary_contract_impl.summary_shaper"):
        out = validate_and_shape_summary(
            _minimal_summary_payload(
                confidence=0.7,
                hallucination_risk="med",
                source_type="marketing-blast",
                temporal_freshness="someday",
            )
        )

    assert out["source_type"] == "unknown"
    assert out["temporal_freshness"] == "unknown"
    assert "summary_source_type_invalid" in caplog.messages
    assert "summary_temporal_freshness_invalid" in caplog.messages


def test_malformed_quality_fields_create_persisted_validation_warnings():
    out = validate_and_shape_summary(
        _minimal_summary_payload(
            confidence="very sure",
            hallucination_risk="definitely no risk",
            source_type="marketing-blast",
            temporal_freshness="someday",
            summary_quality={
                "source_coverage": "mostly",
                "extraction_confidence": "high",
            },
        )
    )

    warnings = set(out["summary_quality"]["validation_warnings"])
    assert {
        "confidence_invalid",
        "hallucination_risk_invalid",
        "source_type_invalid",
        "temporal_freshness_invalid",
        "source_coverage_invalid",
        "extraction_confidence_invalid",
    }.issubset(warnings)
    assert out["summary_quality"]["source_coverage"] == "unknown"


def test_default_summary_contract_descriptor_matches_current_wire_schema():
    descriptor = get_summary_contract_descriptor()

    assert descriptor.contract_id == "default"
    assert descriptor.schema_name == "summary_schema"
    assert descriptor.schema_loader() == get_summary_json_schema()
    assert descriptor.response_format("json_schema") == {
        "type": "json_schema",
        "json_schema": {
            "name": "summary_schema",
            "schema": get_summary_json_schema(),
            "strict": True,
        },
    }
    assert descriptor.response_format("json_object") == {"type": "json_object"}
    assert descriptor.repair_response_format() == {"type": "json_object"}


def test_default_summary_contract_compatibility_mapper_is_current_validator():
    payload = _minimal_summary_payload(topic_tags=["Tech", "tech"])
    descriptor = get_summary_contract_descriptor()

    assert descriptor.compatibility_mapper(payload) == validate_and_shape_summary(payload)


def test_default_summary_contract_prompt_parity_for_en_ru():
    descriptor = get_summary_contract_descriptor()
    manager = PromptManager(validate_on_load=True)

    assert descriptor.supported_languages == ("en", "ru")
    assert manager.get_prompt_version("en") == manager.get_prompt_version("ru")
    assert manager.get_prompt_fields("en") == manager.get_prompt_fields("ru")
    assert manager.get_contract_system_prompt("default", "en", include_examples=False) == (
        descriptor.prompt_loader("en", include_examples=False)
    )
    assert manager.get_contract_system_prompt("default", "ru", include_examples=False) == (
        descriptor.prompt_loader("ru", include_examples=False)
    )
