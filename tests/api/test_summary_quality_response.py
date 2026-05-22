from app.api.routers.content.summaries import _safe_summary_quality


def test_summary_detail_quality_exposes_safe_fields_only() -> None:
    quality = _safe_summary_quality(
        {
            "validation_warnings": ["confidence_invalid"],
            "repair_attempted": True,
            "repair_succeeded": True,
            "structured_output_mode": "json_object",
            "model_used": "safe-model",
            "source_coverage": "partial",
            "extraction_quality": "medium",
            "extraction_confidence": 0.7,
            "prompt_injection_suspected": True,
            "raw_prompt": "must never appear",
            "raw_llm_output": "must never appear",
        }
    ).model_dump(by_alias=True, exclude_none=True)

    assert quality["sourceCoverage"] == "partial"
    assert quality["repairAttempted"] is True
    assert quality["repairSucceeded"] is True
    assert quality["promptInjectionSuspected"] is True
    assert quality["validationWarnings"] == ["confidence_invalid"]
    assert "rawPrompt" not in quality
    assert "raw_prompt" not in quality
    assert "rawLlmOutput" not in quality
    assert "raw_llm_output" not in quality
