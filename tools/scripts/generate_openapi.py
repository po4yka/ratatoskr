"""Generate the committed Mobile API OpenAPI contract from the FastAPI app."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
YAML_PATH = ROOT / "docs" / "openapi" / "mobile_api.yaml"
JSON_PATH = ROOT / "docs" / "openapi" / "mobile_api.json"
sys.path.insert(0, str(ROOT))


def _prepare_import_environment() -> None:
    """Set minimum env required to import the FastAPI app without services."""
    os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost")
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 40)
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    os.environ.setdefault("REDIS_ENABLED", "0")


def generate_spec() -> dict[str, Any]:
    _prepare_import_environment()

    from app.api.main import app

    app.openapi_schema = None
    spec = app.openapi()
    _apply_contract_postprocessing(spec)
    return spec


def _schema_for(model: Any) -> dict[str, Any]:
    return model.model_json_schema(by_alias=True, mode="serialization")


def _rewrite_refs(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "#/components/schemas/" + val.rsplit("/", 1)[-1]
                if key == "$ref" and isinstance(val, str) and val.startswith("#/$defs/")
                else _rewrite_refs(val)
            )
            for key, val in value.items()
            if key != "$defs"
        }
    if isinstance(value, list):
        return [_rewrite_refs(item) for item in value]
    return value


def _add_schema(schemas: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    for def_name, definition in schema.get("$defs", {}).items():
        schemas.setdefault(def_name, _rewrite_refs(definition))
    schemas[name] = _rewrite_refs(schema)


def _force_required(schema: dict[str, Any], fields: set[str]) -> None:
    existing = list(schema.get("required", []))
    for field in fields:
        if field not in existing:
            existing.append(field)
    schema["required"] = existing


def _stream_event_schema(kind: str, payload_ref: str) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["kind", "payload", "timestamp", "correlation_id"],
        "properties": {
            "kind": {"type": "string", "const": kind},
            "payload": {"$ref": payload_ref},
            "timestamp": {"type": "string", "format": "date-time"},
            "correlation_id": {"type": "string"},
        },
    }


def _mark_article_alias_descriptions(spec: dict[str, Any]) -> None:
    """Annotate generated /v1/articles/* paths as aliases of /v1/summaries/*."""
    for path, methods in spec.get("paths", {}).items():
        if not path.startswith("/v1/articles"):
            continue
        canonical_path = path.replace("/v1/articles", "/v1/summaries", 1)
        if canonical_path not in spec.get("paths", {}):
            continue
        for method, operation in methods.items():
            method_upper = method.upper()
            if method_upper not in {"GET", "POST", "PATCH", "DELETE", "PUT", "HEAD"}:
                continue
            operation["description"] = (
                f"Alias for {method_upper} {canonical_path}. "
                f"{operation.get('description') or operation.get('summary') or ''}"
            ).strip()


def _mark_health_probe_descriptions(spec: dict[str, Any]) -> None:
    """Annotate health routes as the documented probe-envelope carve-out."""
    health_paths = {"/health", "/health/live", "/health/ready", "/health/detailed"}
    note = (
        "Health/probe contract carve-out: status code and endpoint-specific probe schema are "
        "authoritative; readiness failure may return a raw probe object instead of the "
        "standard business-response envelope. See docs/decisions/0019-health-endpoint-envelope-carveout.md."
    )
    for path in health_paths:
        for operation in spec.get("paths", {}).get(path, {}).values():
            existing = operation.get("description") or operation.get("summary") or ""
            if note not in existing:
                operation["description"] = f"{note}\n\n{existing}".strip()


def _success_envelope_schema(data_ref: str) -> dict[str, Any]:
    return {
        "allOf": [
            {"$ref": "#/components/schemas/BaseSuccessResponse"},
            {
                "type": "object",
                "required": ["data"],
                "properties": {"data": {"$ref": data_ref}},
            },
        ]
    }


def _apply_contract_postprocessing(spec: dict[str, Any]) -> None:
    """Add contract metadata FastAPI cannot infer from raw-dict handlers yet."""
    from app.adapters.content.streaming.events import (
        DonePayload,
        ErrorPayload,
        SectionPayload,
        StagePayload,
        WarningPayload,
    )
    from app.api.exceptions import ErrorCode, ErrorType
    from app.api.models.responses.auth import UserInfo
    from app.api.models.responses.collections import CollectionItem, CollectionResponse
    from app.api.models.responses.common import MetaInfo, PaginationInfo, SystemMetaResponse
    from app.api.models.responses.requests import (
        RequestDetailResponse,
        RequestStatusData,
        RetryRequestResponse,
        SubmitRequestData,
    )
    from app.api.models.responses.summaries import (
        SummaryCompact,
        SummaryContent,
        SummaryDetail,
        SummaryListResponse,
    )
    from app.api.models.responses.user import UserStatsData

    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    responses = components.setdefault("responses", {})
    security_schemes = components.setdefault("securitySchemes", {})

    security_schemes.setdefault(
        "HTTPBearer",
        {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
    )

    _mark_article_alias_descriptions(spec)
    _mark_health_probe_descriptions(spec)

    for name, model in (
        ("Meta", MetaInfo),
        ("Pagination", PaginationInfo),
        ("SystemMeta", SystemMetaResponse),
        ("SubmitRequestData", SubmitRequestData),
        ("RequestStatusData", RequestStatusData),
        ("RequestDetailResponse", RequestDetailResponse),
        ("RetryRequestResponse", RetryRequestResponse),
        ("User", UserInfo),
        ("UserStats", UserStatsData),
        ("SummaryListItem", SummaryCompact),
        ("PaginatedSummariesData", SummaryListResponse),
        ("SummaryDetail", SummaryDetail),
        ("SummaryContent", SummaryContent),
        ("Collection", CollectionResponse),
        ("CollectionItem", CollectionItem),
        ("StreamStagePayload", StagePayload),
        ("StreamSectionPayload", SectionPayload),
        ("StreamWarningPayload", WarningPayload),
        ("StreamDonePayload", DonePayload),
        ("StreamErrorPayload", ErrorPayload),
    ):
        _add_schema(schemas, name, _schema_for(model))

    if "$ref" in schemas["Collection"]:
        schemas["Collection"] = dict(schemas["CollectionResponse"])

    _force_required(schemas["Meta"], {"api_version"})
    _force_required(schemas["RequestStatusData"], {"canRetry"})
    _force_required(schemas["User"], {"isOwner"})
    _force_required(schemas["Collection"], {"isShared"})

    schemas.update(
        {
            "BaseSuccessResponse": {
                "type": "object",
                "required": ["success", "meta"],
                "properties": {
                    "success": {"type": "boolean", "const": True},
                    "meta": {"$ref": "#/components/schemas/Meta"},
                },
            },
            "SuccessResponse": {
                "allOf": [
                    {"$ref": "#/components/schemas/BaseSuccessResponse"},
                    {
                        "type": "object",
                        "required": ["data"],
                        "properties": {"data": {}},
                    },
                ]
            },
            "ErrorObject": {
                "type": "object",
                "required": ["code", "errorType", "message", "retryable", "correlation_id"],
                "properties": {
                    "code": {"type": "string", "enum": [code.value for code in ErrorCode]},
                    "errorType": {"type": "string", "enum": [kind.value for kind in ErrorType]},
                    "message": {"type": "string"},
                    "retryable": {"type": "boolean"},
                    "details": {"type": "object", "additionalProperties": True, "nullable": True},
                    "correlation_id": {"type": "string"},
                },
            },
            "ErrorResponse": {
                "type": "object",
                "required": ["success", "error", "meta"],
                "properties": {
                    "success": {"type": "boolean", "const": False},
                    "error": {"$ref": "#/components/schemas/ErrorObject"},
                    "meta": {"$ref": "#/components/schemas/Meta"},
                },
            },
            "SubmitRequestSuccessResponse": _success_envelope_schema(
                "#/components/schemas/SubmitRequestData"
            ),
            "RequestStatusSuccessResponse": _success_envelope_schema(
                "#/components/schemas/RequestStatusData"
            ),
            "SystemMetaSuccessResponse": _success_envelope_schema(
                "#/components/schemas/SystemMeta"
            ),
            "RequestDetailSuccessResponse": _success_envelope_schema(
                "#/components/schemas/RequestDetailResponse"
            ),
            "SummaryDetailSuccessResponse": _success_envelope_schema(
                "#/components/schemas/SummaryDetail"
            ),
            "RetryRequestSuccessResponse": _success_envelope_schema(
                "#/components/schemas/RetryRequestResponse"
            ),
            "StreamStageEvent": _stream_event_schema(
                "stage", "#/components/schemas/StreamStagePayload"
            ),
            "StreamSectionEvent": _stream_event_schema(
                "section", "#/components/schemas/StreamSectionPayload"
            ),
            "StreamWarningEvent": _stream_event_schema(
                "warning", "#/components/schemas/StreamWarningPayload"
            ),
            "StreamDoneEvent": _stream_event_schema(
                "done", "#/components/schemas/StreamDonePayload"
            ),
            "StreamErrorEvent": _stream_event_schema(
                "error", "#/components/schemas/StreamErrorPayload"
            ),
        }
    )

    for name, description in (
        ("UnauthorizedError", "Authentication required or invalid."),
        ("ForbiddenError", "Authenticated user is not allowed to perform this action."),
        ("ValidationError", "Request validation failed."),
        ("InternalServerError", "Unexpected server error."),
    ):
        responses.setdefault(
            name,
            {
                "description": description,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                    }
                },
            },
        )

    public_routes = {
        ("GET", "/health"),
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/v1/meta"),
        ("POST", "/v1/auth/credentials-login"),
        ("POST", "/v1/auth/refresh"),
        ("POST", "/v1/auth/secret-login"),
        ("POST", "/v1/auth/telegram-login"),
        ("GET", "/v1/users/me/feed.xml"),
    }
    owner_prefixes = ("/v1/admin/", "/v1/system/")

    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            method_upper = method.upper()
            if method_upper not in {"GET", "POST", "PATCH", "DELETE", "PUT", "HEAD"}:
                continue
            responses_for_operation = operation.setdefault("responses", {})
            if (method_upper, path) not in public_routes:
                operation.setdefault("security", [{"HTTPBearer": []}])
                responses_for_operation.setdefault(
                    "401", {"$ref": "#/components/responses/UnauthorizedError"}
                )
                responses_for_operation.setdefault(
                    "500", {"$ref": "#/components/responses/InternalServerError"}
                )
            if any(path.startswith(prefix) for prefix in owner_prefixes):
                responses_for_operation.setdefault(
                    "403", {"$ref": "#/components/responses/ForbiddenError"}
                )
            responses_for_operation.setdefault(
                "422", {"$ref": "#/components/responses/ValidationError"}
            )


def _render_yaml(spec: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120)
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _render_json(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _check(path: Path, expected: str) -> bool:
    actual = path.read_text() if path.exists() else None
    if actual == expected:
        return True
    print(
        f"OpenAPI drift: {path.relative_to(ROOT)} is not generated from app.api.main:app",
        file=sys.stderr,
    )
    print("Run `make generate-openapi` and commit the updated spec.", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if committed YAML/JSON differs from generated output",
    )
    args = parser.parse_args()

    spec = generate_spec()
    yaml_text = _render_yaml(spec)
    json_text = _render_json(spec)

    if args.check:
        return 0 if _check(YAML_PATH, yaml_text) and _check(JSON_PATH, json_text) else 1

    _write_if_changed(YAML_PATH, yaml_text)
    _write_if_changed(JSON_PATH, json_text)
    print(f"Generated {YAML_PATH.relative_to(ROOT)} and {JSON_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
