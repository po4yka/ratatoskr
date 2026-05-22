"""OpenAPI contract tests for request lifecycle response vocabulary."""

from __future__ import annotations

from pathlib import Path

import yaml


def _schema() -> dict:
    spec = yaml.safe_load(Path("docs/openapi/mobile_api.yaml").read_text())
    return spec["components"]["schemas"]


def test_openapi_exposes_canonical_request_lifecycle_schemas() -> None:
    schemas = _schema()

    assert schemas["RequestStatus"]["enum"] == [
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ]
    assert schemas["ProcessingStage"]["enum"] == [
        "queued",
        "extracting",
        "summarizing",
        "validating",
        "persisting",
        "done",
    ]
    assert schemas["StreamStageEvent"]["properties"]["kind"]["const"] == "stage"
    assert schemas["StreamWarningEvent"]["properties"]["kind"]["const"] == "warning"
    assert "StreamPhaseEvent" not in schemas


def test_openapi_request_responses_preserve_legacy_status_field() -> None:
    schemas = _schema()

    assert "legacyStatus" in schemas["SubmitRequestResponse"]["properties"]
    assert "legacyStatus" in schemas["RequestStatusData"]["properties"]
    assert "legacyStatus" in schemas["RequestDetailRequest"]["properties"]
    assert "legacyStatus" in schemas["RetryRequestResponse"]["properties"]


def test_openapi_request_paths_reference_typed_success_envelopes() -> None:
    spec = yaml.safe_load(Path("docs/openapi/mobile_api.yaml").read_text())

    expected_refs = {
        ("post", "/v1/requests"): "#/components/schemas/SubmitRequestSuccessResponse",
        ("get", "/v1/requests/{request_id}"): "#/components/schemas/RequestDetailSuccessResponse",
        (
            "get",
            "/v1/requests/{request_id}/status",
        ): "#/components/schemas/RequestStatusSuccessResponse",
        (
            "post",
            "/v1/requests/{request_id}/retry",
        ): "#/components/schemas/RetryRequestSuccessResponse",
    }
    for (method, path), expected_ref in expected_refs.items():
        response_schema = spec["paths"][path][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert response_schema["$ref"] == expected_ref
