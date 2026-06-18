"""Strict drift test: runtime-generated OpenAPI vs committed YAML.

The existing tests in ``test_openapi_sync.py`` compare ``app.routes`` (from
FastAPI's internal route registry) against ``docs/openapi/mobile_api.yaml``.
That catches routes registered without YAML coverage, but it does NOT exercise
``app.openapi()`` -- which is the actual contract used by clients fetching
``/openapi.json`` at runtime. A broken ``app.openapi()`` (e.g. unresolved
forward refs in dependency callables) would slip through silently because the
old tests never invoke the spec generator.

This module closes that gap by:

* Calling ``app.openapi()`` directly to produce the runtime spec (and asserting
  it does not raise).
* Comparing the (METHOD, path) set against the YAML spec **in both directions**
  so neither code-only nor YAML-only routes go unnoticed.
* Comparing response envelope schema names so generated docs and codegen remain
  aligned with runtime FastAPI schema output.

The test fails with a precise diff so the offending operation or schema is
obvious from CI output.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped,unused-ignore]
from starlette.routing import Match

# Re-use the same constants as the existing sync tests so we stay consistent
# with the ignore list and the spec-resolution behaviour.
from tests.api.test_openapi_sync import (
    IGNORED_APP_ROUTES,
    RELEVANT_METHODS,
    SPEC_PATH,
)

# FastAPI's OpenAPI generator emits paths without the Starlette ``:converter``
# suffix (e.g. ``/web/{path:path}`` becomes ``/web/{path}``), while the
# ``app.routes`` view (used by the legacy tests and by IGNORED_APP_ROUTES)
# keeps the converter. Normalise both sides before comparing so this purely
# cosmetic difference doesn't trigger false positives.
_PATH_CONVERTER_RE = re.compile(r"\{([^}:]+):[^}]+\}")


def _strip_path_converters(path: str) -> str:
    return _PATH_CONVERTER_RE.sub(r"{\1}", path)


def _normalise(routes: set[tuple[str, str]]) -> set[tuple[str, str]]:
    return {(m, _strip_path_converters(p)) for m, p in routes}


def _extract_runtime_routes(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """Return {(METHOD, path)} from ``app.openapi()`` output, ignoring meta routes."""
    routes: set[tuple[str, str]] = set()
    for path, methods in spec.get("paths", {}).items():
        for method in methods:
            method_upper = method.upper()
            if method_upper in RELEVANT_METHODS:
                routes.add((method_upper, path))
    # ``IGNORED_APP_ROUTES`` uses Starlette-style paths (with ``:converter``);
    # the runtime spec uses the stripped form. Normalise both before diffing.
    return routes - _normalise(set(IGNORED_APP_ROUTES))


def _extract_yaml_routes(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """Return {(METHOD, path)} from the hand-written YAML spec."""
    routes: set[tuple[str, str]] = set()
    for path, methods in spec.get("paths", {}).items():
        for method in methods:
            method_upper = method.upper()
            if method_upper in RELEVANT_METHODS:
                routes.add((method_upper, path))
    return routes


# Runtime-only schemas that are not part of the published API contract:
# * ``Body_*``: anonymous body wrappers FastAPI generates for multi-param bodies
# * ``ValidationError`` / ``HTTPValidationError``: stock 422 schemas that the
#   YAML chooses to express as inline error responses rather than named
#   components. They're framework noise, not surface area worth syncing.
_RUNTIME_NOISE_PREFIXES: tuple[str, ...] = ("Body_",)
_RUNTIME_NOISE_NAMES: frozenset[str] = frozenset({"ValidationError", "HTTPValidationError"})


def _is_runtime_noise(name: str) -> bool:
    return name.startswith(_RUNTIME_NOISE_PREFIXES) or name in _RUNTIME_NOISE_NAMES


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped monkeypatch (pytest's built-in is function-scoped)."""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def runtime_spec(monkeypatch_module) -> dict[str, Any]:
    """Generate the OpenAPI spec from the FastAPI app.

    Spec generation walks all route signatures and Pydantic models; it does
    NOT need a running database or Redis. We set the minimum env vars
    required for ``app.api.main`` to import, then call ``app.openapi()``.
    This deliberately avoids the function-scoped ``client`` fixture (which
    requires Postgres) so the drift test runs in every environment, including
    plain unit-test runs without ``TEST_DATABASE_URL``.
    """
    monkeypatch_module.setenv("ALLOWED_ORIGINS", "http://localhost")
    monkeypatch_module.setenv("JWT_SECRET_KEY", "x" * 40)
    monkeypatch_module.setenv("SECRET_KEY", "x" * 40)
    monkeypatch_module.setenv("REDIS_ENABLED", "0")

    from app.api.main import app

    # Force regeneration in case a previous test left a stale cached schema
    # behind (FastAPI caches ``openapi_schema`` on first call).
    app.openapi_schema = None
    return app.openapi()


@pytest.fixture(scope="module")
def yaml_spec() -> dict[str, Any]:
    with open(SPEC_PATH) as f:
        return yaml.safe_load(f)


class TestRuntimeOpenApiDrift:
    """Runtime ``app.openapi()`` must agree with ``docs/openapi/mobile_api.yaml``."""

    def test_runtime_openapi_is_generatable(self, runtime_spec: dict[str, Any]) -> None:
        """Regression guard: calling ``app.openapi()`` must not raise.

        Pydantic v2 raises ``PydanticUserError`` if any dependency callable
        signature or response model has unresolved forward refs. That used to
        slip past the existing sync tests because they only walked
        ``app.routes`` without invoking the spec generator.
        """
        assert isinstance(runtime_spec, dict)
        assert runtime_spec.get("openapi", "").startswith("3."), (
            f"runtime_spec.openapi looks malformed: {runtime_spec.get('openapi')!r}"
        )
        assert runtime_spec.get("paths"), "runtime_spec has no paths"

    def test_route_sets_match_bidirectionally(
        self, runtime_spec: dict[str, Any], yaml_spec: dict[str, Any]
    ) -> None:
        """The (METHOD, path) sets must match in BOTH directions.

        The legacy ``TestRouteCoverage`` test only fails on code-not-in-spec;
        spec-not-in-code is a warning. That asymmetry let YAML routes
        accumulate without runtime backing. Here we fail on either direction
        so drift is caught at PR time.
        """
        runtime_routes = _normalise(_extract_runtime_routes(runtime_spec))
        yaml_routes = _normalise(_extract_yaml_routes(yaml_spec))

        only_runtime = runtime_routes - yaml_routes
        only_yaml = yaml_routes - runtime_routes

        errors: list[str] = []
        if only_runtime:
            errors.append(
                "In runtime app.openapi() but NOT in docs/openapi/mobile_api.yaml:\n"
                + "\n".join(f"  {m} {p}" for m, p in sorted(only_runtime))
            )
        if only_yaml:
            errors.append(
                "In docs/openapi/mobile_api.yaml but NOT in runtime app.openapi():\n"
                + "\n".join(f"  {m} {p}" for m, p in sorted(only_yaml))
            )

        if errors:
            pytest.fail(
                "Route drift between runtime app.openapi() and YAML:\n" + "\n\n".join(errors)
            )

    def test_static_runtime_routes_are_not_shadowed_by_dynamic_routes(
        self, runtime_spec: dict[str, Any]
    ) -> None:
        """A documented static path must dispatch to its own route at runtime.

        FastAPI's OpenAPI generator lists every route independently, but
        Starlette dispatches by declaration order. If an earlier dynamic route
        such as ``/{id}`` can match a later static route like ``/search``, the
        runtime returns the dynamic route's validation error instead of the
        static route contract even though OpenAPI advertises the static path.
        """
        from app.api.main import app

        documented_static_routes = {
            (method.upper(), path)
            for path, methods in runtime_spec.get("paths", {}).items()
            if "{" not in path
            for method in methods
            if method.upper() in RELEVANT_METHODS
        }

        shadows: list[str] = []
        seen_routes: list[Any] = []
        for route in app.routes:
            route_methods = {m.upper() for m in getattr(route, "methods", set())}
            route_path = _strip_path_converters(getattr(route, "path", ""))
            for method in route_methods & RELEVANT_METHODS:
                if (method, route_path) in documented_static_routes:
                    scope = {
                        "type": "http",
                        "method": method,
                        "path": route_path,
                        "root_path": "",
                    }
                    for previous in seen_routes:
                        previous_methods = {
                            m.upper() for m in getattr(previous, "methods", set())
                        }
                        if method not in previous_methods:
                            continue
                        match, _ = previous.matches(scope)
                        if match is Match.FULL:
                            shadows.append(
                                f"{method} {route_path} is shadowed by "
                                f"{getattr(previous, 'path', '<unknown>')}"
                            )
                            break
            seen_routes.append(route)

        if shadows:
            pytest.fail(
                "Runtime route dispatch shadows documented static OpenAPI paths:\n"
                + "\n".join(f"  - {shadow}" for shadow in shadows)
            )

    def test_runtime_response_envelopes_align_with_yaml(
        self, runtime_spec: dict[str, Any], yaml_spec: dict[str, Any]
    ) -> None:
        """Response envelope names produced by the runtime must exist in YAML.

        FastAPI publishes response schemas under their Pydantic class name.
        Those names are part of the contract that downstream codegen relies
        on (e.g. KMP clients import ``CollectionResponseEnvelope`` directly).
        Verify that every runtime schema whose name ends in a known envelope
        suffix is also declared in the YAML so generators stay in sync.

        Runtime-only noise (``Body_*``, ``ValidationError``,
        ``HTTPValidationError``) is excluded because it is framework
        plumbing, not user-facing.
        """
        runtime_schemas_all = set((runtime_spec.get("components") or {}).get("schemas", {}).keys())
        yaml_schemas = set(yaml_spec.get("components", {}).get("schemas", {}).keys())

        envelope_suffixes = ("ResponseEnvelope", "Envelope")
        runtime_envelopes = {
            s
            for s in runtime_schemas_all
            if not _is_runtime_noise(s) and s.endswith(envelope_suffixes)
        }

        missing_in_yaml = runtime_envelopes - yaml_schemas
        if missing_in_yaml:
            pytest.fail(
                "Runtime response envelopes missing from "
                "docs/openapi/mobile_api.yaml:\n"
                + "\n".join(f"  - {s}" for s in sorted(missing_in_yaml))
            )
