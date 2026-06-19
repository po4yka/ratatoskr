"""
Tests that OpenAPI security declarations match FastAPI route auth dependencies.

Four checks:
  1. Code-protected routes (using get_current_user) declare security in YAML.
  2. YAML-public routes are in an explicit PUBLIC_ROUTES allowlist.
  3. No route in PUBLIC_ROUTES also declares security (stale allowlist guard).
  4. Owner-only routes (/v1/admin/, /v1/system/) document a 403 response.

Run with:
    pytest tests/api/test_openapi_security.py -v
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml  # type: ignore[import-untyped,unused-ignore]

from tests.api.test_openapi_sync import RELEVANT_METHODS, SPEC_PATH

# ---------------------------------------------------------------------------
# Public-route allowlist
# ---------------------------------------------------------------------------
# Routes intentionally documented without a security scheme.
# If a new public route is added, add it here.
# If a route here is later secured, remove it from this set.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/"),
        ("GET", "/health"),
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/v1/meta"),
        ("POST", "/v1/auth/credentials-login"),
        ("POST", "/v1/auth/refresh"),
        ("POST", "/v1/auth/secret-login"),
        ("POST", "/v1/auth/telegram-login"),
        ("GET", "/v1/public/collections/{token}"),
        ("GET", "/v1/users/me/feed.xml"),
    }
)

# Path prefixes whose handlers call require_owner() inside the body.
# We verify these document a 403 response as a proxy for owner-only annotation.
_OWNER_ONLY_PREFIXES: tuple[str, ...] = ("/v1/admin/", "/v1/system/")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _depends_on(dependant: Any, target_fn: Any, _seen: set[int] | None = None) -> bool:
    """Return True if target_fn appears anywhere in the FastAPI Dependant tree."""
    if _seen is None:
        _seen = set()
    dep_id = id(dependant)
    if dep_id in _seen:
        return False
    _seen.add(dep_id)
    if dependant.call is target_fn:
        return True
    return any(_depends_on(dep, target_fn, _seen) for dep in dependant.dependencies)


def _get_code_protected_routes(app: Any, protected_fn: Any) -> set[tuple[str, str]]:
    """Return (METHOD, path) pairs for routes that depend on protected_fn."""
    protected: set[tuple[str, str]] = set()
    for route in app.routes:
        if not (
            hasattr(route, "dependant") and hasattr(route, "methods") and hasattr(route, "path")
        ):
            continue
        if _depends_on(route.dependant, protected_fn):
            for method in route.methods:
                if method.upper() in RELEVANT_METHODS:
                    protected.add((method.upper(), route.path))
    return protected


def _get_spec_public_routes(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """Return (METHOD, path) pairs for YAML operations with no security declaration."""
    public: set[tuple[str, str]] = set()
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if method.upper() not in RELEVANT_METHODS:
                continue
            if not operation.get("security"):
                public.add((method.upper(), path))
    return public


def _get_spec_protected_routes(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """Return (METHOD, path) pairs for YAML operations that declare HTTPBearer security."""
    protected: set[tuple[str, str]] = set()
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if method.upper() not in RELEVANT_METHODS:
                continue
            security = operation.get("security") or []
            if any(isinstance(s, dict) and "HTTPBearer" in s for s in security):
                protected.add((method.upper(), path))
    return protected


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _monkeypatch_module():
    """Module-scoped monkeypatch (pytest's built-in is function-scoped)."""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def app_instance(_monkeypatch_module: Any) -> Any:
    """FastAPI app with minimal env vars — no DB or Redis needed."""
    _monkeypatch_module.setenv("ALLOWED_ORIGINS", "http://localhost")
    _monkeypatch_module.setenv("JWT_SECRET_KEY", "x" * 40)
    _monkeypatch_module.setenv("SECRET_KEY", "x" * 40)
    _monkeypatch_module.setenv("REDIS_ENABLED", "0")

    from app.api.main import app

    return app


@pytest.fixture(scope="module")
def yaml_spec() -> dict[str, Any]:
    with open(SPEC_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSecurityConsistency:
    """OpenAPI security declarations must match FastAPI auth dependencies."""

    def test_protected_routes_declare_security(
        self, app_instance: Any, yaml_spec: dict[str, Any]
    ) -> None:
        """Every route using get_current_user must declare HTTPBearer security in YAML."""
        from app.api.routers.auth import get_current_user

        code_protected = _get_code_protected_routes(app_instance, get_current_user)
        spec_paths = yaml_spec.get("paths", {})

        failures: list[str] = []
        for method, path in sorted(code_protected):
            operation = spec_paths.get(path, {}).get(method.lower())
            if operation is None:
                continue  # Missing route handled by test_openapi_sync.py
            security = operation.get("security") or []
            has_bearer = any(isinstance(s, dict) and "HTTPBearer" in s for s in security)
            if not has_bearer:
                failures.append(f"  {method} {path}")

        if failures:
            pytest.fail(
                f"{len(failures)} code-protected route(s) missing security declaration in YAML:\n"
                + "\n".join(failures)
                + "\n\nFix: add the following to each operation in docs/openapi/mobile_api.yaml:\n"
                + "  security:\n    - HTTPBearer: []"
            )

    def test_public_routes_are_allowlisted(self, yaml_spec: dict[str, Any]) -> None:
        """Every YAML operation without security must be in PUBLIC_ROUTES."""
        spec_public = _get_spec_public_routes(yaml_spec)
        unrecognized = spec_public - PUBLIC_ROUTES

        if unrecognized:
            formatted = "\n".join(f"  {m} {p}" for m, p in sorted(unrecognized))
            pytest.fail(
                f"{len(unrecognized)} public route(s) not in the PUBLIC_ROUTES allowlist:\n"
                + formatted
                + "\n\nFix: either add 'security:\\n  - HTTPBearer: []' to the YAML operation, "
                + "or add the route to PUBLIC_ROUTES in tests/api/test_openapi_security.py."
            )

    def test_allowlisted_routes_are_not_secured(self, yaml_spec: dict[str, Any]) -> None:
        """No PUBLIC_ROUTES entry should also declare security (stale allowlist guard)."""
        spec_protected = _get_spec_protected_routes(yaml_spec)
        stale = PUBLIC_ROUTES & spec_protected

        if stale:
            formatted = "\n".join(f"  {m} {p}" for m, p in sorted(stale))
            pytest.fail(
                f"{len(stale)} route(s) are in PUBLIC_ROUTES but also declare security in YAML:\n"
                + formatted
                + "\n\nFix: remove the route from PUBLIC_ROUTES in "
                + "tests/api/test_openapi_security.py."
            )

    def test_owner_only_routes_document_403(self, yaml_spec: dict[str, Any]) -> None:
        """Routes under /v1/admin/ and /v1/system/ must document a 403 response."""
        failures: list[str] = []

        for path, methods in yaml_spec.get("paths", {}).items():
            if not any(path.startswith(prefix) for prefix in _OWNER_ONLY_PREFIXES):
                continue
            for method, operation in methods.items():
                if method.upper() not in RELEVANT_METHODS:
                    continue
                responses = {str(k) for k in operation.get("responses", {})}
                if "403" not in responses:
                    failures.append(f"  {method.upper()} {path}")

        if failures:
            pytest.fail(
                f"{len(failures)} owner-only route(s) missing '403' response in YAML:\n"
                + "\n".join(failures)
                + "\n\nFix: add a 403 response to each operation in docs/openapi/mobile_api.yaml:\n"
                + "  '403':\n    $ref: '#/components/responses/ForbiddenError'"
            )
