"""OpenAPI guards for operational JSON and binary response contracts."""

from __future__ import annotations


def test_operational_json_routes_publish_typed_success_data() -> None:
    from app.api.main import app

    app.openapi_schema = None
    spec = app.openapi()
    routes = {
        ("get", "/v1/admin/users"),
        ("get", "/v1/admin/jobs"),
        ("get", "/v1/admin/health/content"),
        ("get", "/v1/admin/metrics"),
        ("get", "/v1/admin/llm-costs"),
        ("get", "/v1/admin/audit-log"),
        ("get", "/v1/system/db-info"),
        ("post", "/v1/system/clear-cache"),
        ("get", "/health/detailed"),
        ("get", "/health/ready"),
        ("get", "/health/live"),
    }

    for method, path in routes:
        schema = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert "$ref" in schema, f"{method.upper()} {path} has no typed response"


def test_database_dump_routes_publish_binary_media_contract() -> None:
    from app.api.main import app

    app.openapi_schema = None
    spec = app.openapi()

    for method in ("get", "head"):
        content = spec["paths"]["/v1/system/db-dump"][method]["responses"]["200"]["content"]
        assert content == {
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
        }
