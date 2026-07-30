from __future__ import annotations

import json
import os
import subprocess
import sys

import yaml

from app.api.models.responses.common import API_CONTRACT_VERSION
from tools.scripts.generate_openapi import (
    JSON_PATH,
    YAML_PATH,
    generate_spec,
)

_HTTP_METHODS = {"DELETE", "GET", "HEAD", "PATCH", "POST", "PUT"}
_INTENTIONAL_NON_JSON_SUCCESS_OPERATIONS = {
    ("GET", "/metrics"),
    ("GET", "/v1/articles/{summary_id}/audio"),
    ("GET", "/v1/articles/{summary_id}/export"),
    ("GET", "/v1/ai-backups/{service}/reauth/{flow_id}/frame"),
    ("GET", "/v1/backups/{backup_id}/download"),
    ("GET", "/v1/digest/runs/{run_id}/stream"),
    ("GET", "/v1/export"),
    ("GET", "/v1/github/syncs/{sync_id}/stream"),
    ("GET", "/v1/proxy/image"),
    ("GET", "/v1/requests/{request_id}/stream"),
    ("GET", "/v1/rss/export/opml"),
    ("GET", "/v1/summaries/{summary_id}/audio"),
    ("GET", "/v1/summaries/{summary_id}/export"),
    ("GET", "/v1/system/db-dump"),
    ("GET", "/v1/users/me/feed.xml"),
    ("GET", "/v1/vector-reconciler/runs/{run_id}/stream"),
    ("HEAD", "/v1/system/db-dump"),
}


def _resolve_schema(spec: dict, schema: dict) -> dict:
    seen: set[str] = set()
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if ref in seen or not ref.startswith("#/components/schemas/"):
            break
        seen.add(ref)
        schema = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    return schema


def _schema_has_named_structure(spec: dict, schema: dict) -> bool:
    schema = _resolve_schema(spec, schema)
    if not isinstance(schema, dict) or not schema:
        return False
    if any(
        _schema_has_named_structure(spec, candidate)
        for key in ("allOf", "anyOf", "oneOf")
        for candidate in schema.get(key, [])
    ):
        return True
    if schema.get("type") == "array":
        return _schema_has_named_structure(spec, schema.get("items", {}))
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return False
    # A standard success envelope is only useful to codegen when its business
    # payload is also typed; ``SuccessResponse[data: Any]`` must still fail.
    if "data" in properties:
        return _schema_has_named_structure(spec, properties["data"])
    return True


def test_generated_openapi_version_matches_contract_version() -> None:
    spec = generate_spec()

    assert spec["info"]["version"] == API_CONTRACT_VERSION


def test_generated_422_responses_use_project_error_envelope() -> None:
    spec = generate_spec()
    expected = {"$ref": "#/components/responses/ValidationError"}

    mismatches = []
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method.upper() not in {"DELETE", "GET", "HEAD", "PATCH", "POST", "PUT"}:
                continue
            if operation["responses"]["422"] != expected:
                mismatches.append(f"{method.upper()} {path}")

    assert mismatches == []
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    assert "ValidationError" not in spec["components"]["schemas"]


def test_generated_json_success_responses_have_business_schemas() -> None:
    spec = generate_spec()
    failures: list[str] = []

    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method.upper() not in _HTTP_METHODS:
                continue
            for status_code, response in operation.get("responses", {}).items():
                if not str(status_code).startswith("2") or not isinstance(response, dict):
                    continue
                json_content = response.get("content", {}).get("application/json")
                if json_content is None:
                    continue
                if not _schema_has_named_structure(spec, json_content.get("schema", {})):
                    failures.append(f"{method.upper()} {path} {status_code}")

    assert failures == [], "Untyped JSON success responses:\n" + "\n".join(failures)


def test_non_json_success_operations_are_explicitly_classified() -> None:
    spec = generate_spec()
    observed: set[tuple[str, str]] = set()

    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method.upper() not in _HTTP_METHODS:
                continue
            responses = operation.get("responses", {})
            success_responses = [
                response
                for status_code, response in responses.items()
                if str(status_code).startswith("2")
                and str(status_code) != "204"
                and isinstance(response, dict)
            ]
            if success_responses and all(
                "application/json" not in response.get("content", {})
                for response in success_responses
            ):
                observed.add((method.upper(), path))

    assert observed == _INTENTIONAL_NON_JSON_SUCCESS_OPERATIONS


def test_committed_openapi_docs_match_generator() -> None:
    env = {
        **os.environ,
        "ALLOWED_ORIGINS": "http://localhost",
        "JWT_SECRET_KEY": "x" * 40,
        "SECRET_KEY": "x" * 40,
        "REDIS_ENABLED": "0",
    }
    result = subprocess.run(
        [sys.executable, "tools/scripts/generate_openapi.py", "--check"],
        cwd=YAML_PATH.parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_committed_openapi_yaml_and_json_are_equivalent() -> None:
    yaml_spec = yaml.safe_load(YAML_PATH.read_text())
    json_spec = json.loads(JSON_PATH.read_text())

    assert yaml_spec == json_spec
