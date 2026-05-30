"""Validation agent for enforcing summary JSON contract."""

from __future__ import annotations

import difflib
import re
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base_agent import AgentResult, BaseAgent, _tracer
from app.core.summary_contract import validate_and_shape_summary
from app.core.summary_contract_impl.common import is_numeric
from app.observability.attributes import (
    AGENT_ATTEMPT,
    AGENT_NAME,
    AGENT_VALIDATION_FAILURE_REASON,
    REQUEST_CORRELATION_ID,
)
from app.observability.metrics import record_agent_validation_failure


class ValidationInput(BaseModel):
    """Input for validation."""

    summary_json: dict[str, Any]


class ValidationOutput(BaseModel):
    """Output from validation."""

    summary_json: dict[str, Any]
    validation_warnings: list[str] = Field(default_factory=list)
    corrections_applied: list[str] = Field(default_factory=list)


class ValidationAgent(BaseAgent[ValidationInput, ValidationOutput]):
    """Agent responsible for enforcing summary JSON contract.

    This agent:
    - Validates all required fields are present
    - Enforces character limits (250, 1000)
    - Checks topic tag formatting (#prefix)
    - Deduplicates entities (case-insensitive)
    - Validates data types and structure
    - Provides detailed error messages for corrections
    """

    def __init__(self, correlation_id: str | None = None):
        super().__init__(name="ValidationAgent", correlation_id=correlation_id)

    async def execute(self, input_data: ValidationInput) -> AgentResult[ValidationOutput]:
        """Validate summary JSON against strict contract."""
        summary = input_data.summary_json

        with _tracer.start_as_current_span("agent.validation") as span:
            span.set_attribute(AGENT_NAME, "validation")
            span.set_attribute(REQUEST_CORRELATION_ID, self.correlation_id)
            span.set_attribute(AGENT_ATTEMPT, 1)

            errors: list[str] = []
            warnings: list[str] = []
            corrections: list[str] = []

            try:
                self._validate_required_fields(summary, errors)
                self._validate_summary_lengths(summary, errors, warnings)
                self._validate_topic_tags(summary, errors, warnings)
                self._validate_entities(summary, errors)
                self._validate_key_stats(summary, errors)
                self._validate_readability(summary, errors, warnings)
                self._validate_reading_time(summary, errors, warnings)
                self._validate_classification_fields(summary, errors)

                cross_field_issues = self._validate_summary_distinctness(summary)
                errors.extend(cross_field_issues["errors"])
                warnings.extend(cross_field_issues["warnings"])

                if errors:
                    error_message = self._format_validation_errors(errors)
                    self.log_error(f"Validation failed: {len(errors)} error(s)")
                    reason = self._classify_validation_failure(errors)
                    span.set_attribute(AGENT_VALIDATION_FAILURE_REASON, reason)
                    record_agent_validation_failure(reason=reason)
                    return AgentResult.error_result(
                        error_message, error_count=len(errors), warnings=warnings
                    )

                validated_summary = validate_and_shape_summary(summary)

                if warnings:
                    self.log_warning(f"Validation warnings: {'; '.join(warnings)}")

                return AgentResult.success_result(
                    ValidationOutput(
                        summary_json=validated_summary,
                        validation_warnings=warnings,
                        corrections_applied=corrections,
                    ),
                    warning_count=len(warnings),
                )

            except Exception as e:
                self.log_error(f"Validation error: {e}")
                record_agent_validation_failure(reason="unknown")
                return AgentResult.error_result(
                    f"Validation exception: {e!s}", exception_type=type(e).__name__
                )

    @staticmethod
    def _classify_validation_failure(errors: list[str]) -> str:
        """Map the first validation error to a low-cardinality reason label."""
        if not errors:
            return "unknown"
        first = errors[0].lower()
        if "missing required field" in first or "missing" in first:
            return "missing_field"
        if "exceeds limit" in first or "exceed" in first or "max" in first:
            return "length_exceeded"
        if "must be" in first or "must have" in first or "must be a" in first:
            return "schema_mismatch"
        if "language" in first:
            return "language_mismatch"
        return "unknown"

    def _validate_required_fields(self, summary: dict[str, Any], errors: list[str]) -> None:
        required_fields = [
            "summary_250",
            "summary_1000",
            "tldr",
            "key_ideas",
            "topic_tags",
            "entities",
            "estimated_reading_time_min",
            "key_stats",
            "answered_questions",
            "readability",
            "seo_keywords",
        ]
        missing_fields = [field for field in required_fields if field not in summary]
        if not missing_fields:
            return
        errors.append(
            f"Missing required fields: {', '.join(missing_fields)}. "
            "Include all listed fields with non-empty values in the JSON output."
        )

    def _validate_summary_lengths(
        self, summary: dict[str, Any], errors: list[str], warnings: list[str]
    ) -> None:
        if "summary_250" in summary:
            length_250 = len(summary["summary_250"])
            if length_250 > 250:
                errors.append(
                    f"summary_250 exceeds limit: {length_250} chars (max 250). "
                    "Rewrite as a single sentence under 250 characters ending at . ! or ?"
                )
            elif length_250 < 50:
                warnings.append(f"summary_250 very short: {length_250} chars")

        if "summary_1000" in summary:
            length_1000 = len(summary["summary_1000"])
            if length_1000 > 1000:
                errors.append(
                    f"summary_1000 exceeds limit: {length_1000} chars (max 1000). "
                    "Rewrite as 3-5 sentences under 1000 characters total."
                )
            elif length_1000 < 100:
                warnings.append(f"summary_1000 very short: {length_1000} chars")

    def _validate_topic_tags(
        self, summary: dict[str, Any], errors: list[str], warnings: list[str]
    ) -> None:
        if "topic_tags" not in summary:
            return
        tags = summary["topic_tags"]
        if not isinstance(tags, list):
            errors.append("topic_tags must be a list")
            return
        invalid_tags = [t for t in tags if not str(t).startswith("#")]
        if invalid_tags:
            errors.append(
                f"Topic tags missing '#' prefix: {', '.join(invalid_tags)}. "
                "Each tag must be lowercase with # prefix, e.g. #machine-learning"
            )
        if len(tags) > 10:
            warnings.append(f"Many topic tags: {len(tags)} (recommend ≤10)")

    def _validate_entities(self, summary: dict[str, Any], errors: list[str]) -> None:
        if "entities" not in summary:
            return
        entities = summary["entities"]
        if not isinstance(entities, dict):
            errors.append("entities must be a dictionary")
            return
        for entity_type in ("people", "organizations", "locations"):
            if entity_type not in entities:
                errors.append(f"entities.{entity_type} is required")
            elif not isinstance(entities[entity_type], list):
                errors.append(f"entities.{entity_type} must be a list")

    def _validate_key_stats(self, summary: dict[str, Any], errors: list[str]) -> None:
        if "key_stats" not in summary:
            return
        stats = summary["key_stats"]
        if not isinstance(stats, list):
            errors.append("key_stats must be a list")
            return
        for idx, stat in enumerate(stats):
            if not isinstance(stat, dict):
                errors.append(f"key_stats[{idx}] must be a dictionary")
                continue
            if "label" not in stat:
                errors.append(f"key_stats[{idx}] missing 'label' field")
            if "value" not in stat:
                errors.append(f"key_stats[{idx}] missing 'value' field")
            elif not is_numeric(stat["value"]):
                errors.append(
                    f"key_stats[{idx}].value must be numeric, got {type(stat['value']).__name__}. "
                    "value must be a number (42, 3.14), not a string like 'N/A'"
                )

    def _validate_readability(
        self, summary: dict[str, Any], errors: list[str], warnings: list[str]
    ) -> None:
        if "readability" not in summary:
            return
        readability = summary["readability"]
        if not isinstance(readability, dict):
            errors.append("readability must be a dictionary")
            return
        if "score" not in readability:
            errors.append(
                "readability.score is required. Provide a numeric Flesch-Kincaid score, e.g. 65.0"
            )
        elif not isinstance(readability["score"], int | float):
            errors.append(
                "readability.score must be numeric. "
                "Provide a Flesch-Kincaid score as a number, e.g. 65.0"
            )
        if "level" not in readability:
            warnings.append("readability.level missing (recommended)")

    def _validate_reading_time(
        self, summary: dict[str, Any], errors: list[str], warnings: list[str]
    ) -> None:
        if "estimated_reading_time_min" not in summary:
            return
        reading_time = summary["estimated_reading_time_min"]
        if not isinstance(reading_time, int):
            errors.append(
                f"estimated_reading_time_min must be integer, got {type(reading_time).__name__}. "
                "Provide a whole number of minutes, e.g. 5"
            )
        elif reading_time < 1:
            warnings.append(f"estimated_reading_time_min very low: {reading_time}")

    def _validate_classification_fields(self, summary: dict[str, Any], errors: list[str]) -> None:
        valid_source_types = {"news", "blog", "research", "opinion", "tutorial", "reference"}
        if "source_type" in summary:
            source_type = str(summary["source_type"]).lower()
            if source_type not in valid_source_types:
                errors.append(
                    f"source_type must be one of: {', '.join(sorted(valid_source_types))}. "
                    f"Got: '{summary['source_type']}'. "
                    "Choose exactly one value from the allowed list."
                )

        valid_freshness = {"breaking", "recent", "evergreen"}
        if "temporal_freshness" in summary:
            freshness = str(summary["temporal_freshness"]).lower()
            if freshness not in valid_freshness:
                errors.append(
                    f"temporal_freshness must be one of: {', '.join(sorted(valid_freshness))}. "
                    f"Got: '{summary['temporal_freshness']}'. "
                    "Choose exactly one value from the allowed list."
                )

    def _format_validation_errors(self, errors: list[str]) -> str:
        if len(errors) == 1:
            return errors[0]

        formatted = f"Found {len(errors)} validation errors:\n"
        for idx, error in enumerate(errors, 1):
            formatted += f"{idx}. {error}\n"

        return formatted.strip()

    def _validate_summary_distinctness(self, summary: dict[str, Any]) -> dict[str, list[str]]:
        errors: list[str] = []
        warnings: list[str] = []

        summary_250 = str(summary.get("summary_250", "")).strip()
        summary_1000 = str(summary.get("summary_1000", "")).strip()
        tldr = str(summary.get("tldr", "")).strip()

        if not (summary_250 and summary_1000 and tldr):
            return {"errors": errors, "warnings": warnings}

        # Check similarity using difflib
        def similarity_ratio(a: str, b: str) -> float:
            return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

        # Check for high similarity
        sim_250_1000 = similarity_ratio(summary_250, summary_1000)
        sim_1000_tldr = similarity_ratio(summary_1000, tldr)
        sim_250_tldr = similarity_ratio(summary_250, tldr)

        if sim_250_1000 > 0.85:
            warnings.append(
                f"summary_250 and summary_1000 are very similar ({sim_250_1000:.0%}). "
                "Consider making them more distinct."
            )

        if sim_1000_tldr > 0.90:
            errors.append(
                f"summary_1000 and tldr are too similar ({sim_1000_tldr:.0%}). "
                "tldr should expand on summary_1000 with additional details."
            )

        if sim_250_tldr > 0.80:
            warnings.append(
                f"summary_250 and tldr are quite similar ({sim_250_tldr:.0%}). "
                "Consider making them more distinct."
            )

        # Check for exact sentence overlap
        def extract_sentences(text: str) -> set[str]:
            sentences = re.split(r"[.!?]+", text)
            return {s.strip().lower() for s in sentences if len(s.strip()) > 20}

        sentences_250 = extract_sentences(summary_250)
        sentences_1000 = extract_sentences(summary_1000)
        sentences_tldr = extract_sentences(tldr)

        overlap_250_1000 = sentences_250 & sentences_1000
        overlap_1000_tldr = sentences_1000 & sentences_tldr

        if overlap_250_1000:
            warnings.append(
                f"Found {len(overlap_250_1000)} identical sentence(s) "
                "between summary_250 and summary_1000."
            )

        if len(overlap_1000_tldr) > 2:
            warnings.append(
                f"Found {len(overlap_1000_tldr)} identical sentence(s) "
                "between summary_1000 and tldr. tldr should use different phrasing."
            )

        # Check that tldr is longer than summary_1000.
        # Skip when tldr was shaper-backfilled from summary_1000 or summary_250
        # (those cases are intentional and not a contract violation).
        if len(tldr) <= len(summary_1000) and tldr not in (summary_1000, summary_250):
            warnings.append(
                f"tldr ({len(tldr)} chars) should be longer than "
                f"summary_1000 ({len(summary_1000)} chars)."
            )

        return {"errors": errors, "warnings": warnings}
