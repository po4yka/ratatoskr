"""Every /v1 success response must ship the standard {success, data, meta} envelope.

The Mobile API has two response conventions, and only one of them is real:
`success_response()` wraps the payload, and every client -- the web SPA's
`apiRequest` above all -- refuses a body without `success`. A router that
returns its bare `response_model` therefore answers HTTP 200 with a body the
client rejects as an error, which is invisible to both sides' unit tests: the
web mocks `apiRequest`, and the backend asserts the payload model, so nothing
exercises the seam between them.

This guard closes that gap by reading the generated contract itself. It runs
against the committed spec, which `make check-openapi-drift` already pins to
`app.api.main:app`, so a route that drops the envelope fails here rather than
in the browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi" / "mobile_api.json"

ENVELOPE_FIELDS = {"success", "data", "meta"}

# 204 carries no body; clients short-circuit it before parsing.
_NO_BODY_STATUS = "204"
_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _property_names(schema: Any, components: dict[str, Any], depth: int = 0) -> set[str]:
    """Property names of a schema, following $ref and merging allOf branches.

    Envelope models reach OpenAPI as `allOf: [BaseSuccessResponse, {data: X}]`,
    so a check that only reads `properties` would see nothing and report every
    correctly-enveloped route as a violation.
    """
    if not isinstance(schema, dict) or depth > 10:
        return set()
    ref = schema.get("$ref")
    if ref:
        return _property_names(components.get(ref.split("/")[-1], {}), components, depth + 1)
    names = set(schema.get("properties") or {})
    for branch in schema.get("allOf") or ():
        names |= _property_names(branch, components, depth + 1)
    return names


def test_v1_success_responses_are_enveloped() -> None:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    components = spec.get("components", {}).get("schemas", {})

    violations: list[str] = []
    for path, operations in sorted(spec.get("paths", {}).items()):
        if not path.startswith("/v1/"):
            continue
        for method, operation in operations.items():
            if method not in _HTTP_METHODS:
                continue
            for status, response in (operation.get("responses") or {}).items():
                if not status.startswith("2") or status == _NO_BODY_STATUS:
                    continue
                # Streaming responses (text/event-stream) are deliberately raw.
                schema = ((response.get("content") or {}).get("application/json") or {}).get(
                    "schema"
                )
                if schema is None:
                    continue
                if not _property_names(schema, components) >= ENVELOPE_FIELDS:
                    violations.append(f"{method.upper()} {path} [{status}]")

    assert not violations, (
        f"{len(violations)} /v1 response(s) return a bare payload instead of the "
        "{success, data, meta} envelope. Wrap the handler in success_response() and "
        "declare response_model=TypedSuccessResponse[...]:\n  " + "\n  ".join(violations)
    )
