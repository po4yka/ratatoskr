import pytest
from pydantic import HttpUrl, TypeAdapter

from app.api.models.requests import SubmitURLRequest
from app.api.models.responses import SuccessResponse
from app.core.summary_contract import validate_and_shape_summary

pytestmark = pytest.mark.contracts


def test_mobile_request_contract_defaults() -> None:
    input_url: HttpUrl = TypeAdapter(HttpUrl).validate_python("https://example.com/article")
    payload = SubmitURLRequest(input_url=input_url)

    assert payload.type == "url"
    assert str(payload.input_url) == "https://example.com/article"
    assert payload.lang_preference == "auto"


def test_mobile_response_contract_shape() -> None:
    response = SuccessResponse(data={"request_id": 1, "status": "ok"})

    dumped = response.model_dump(mode="json")
    assert dumped["success"] is True
    assert dumped["data"]["request_id"] == 1
    assert "meta" in dumped
    assert "timestamp" in dumped["meta"]


def test_summary_json_contract_freeze_fields() -> None:
    shaped = validate_and_shape_summary(
        {
            "summary_250": "Compact summary.",
            "summary_1000": "Expanded summary with key details.",
            "topic_tags": ["Tech", "tech", "rust"],
            "entities": {"people": ["Alice", "alice"]},
        }
    )

    assert shaped["summary_250"] == "Compact summary."
    assert shaped["summary_1000"] == "Expanded summary with key details."
    assert shaped["topic_tags"] == ["#Tech", "#rust"]
    assert shaped["entities"]["people"] == ["Alice"]
    assert "semantic_chunks" in shaped
    assert "query_expansion_keywords" in shaped
