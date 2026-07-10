"""Tests for security_headers_middleware, in particular the CSP split between
the SPA/API path class and the opt-in FastAPI docs UI path class."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.api.middleware import security_headers_middleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(security_headers_middleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.get("/docs")
    async def docs():
        return PlainTextResponse("docs stub")

    @app.get("/docs/oauth2-redirect")
    async def docs_oauth2_redirect():
        return PlainTextResponse("oauth2 redirect stub")

    @app.get("/redoc")
    async def redoc():
        return PlainTextResponse("redoc stub")

    @app.get("/openapi.json")
    async def openapi_json():
        return {"openapi": "3.1.0"}

    @app.get("/preset")
    async def preset():
        return PlainTextResponse(
            "preset", headers={"Content-Security-Policy": "default-src 'none'"}
        )

    return app


class TestBaselineHeaders:
    def test_common_security_headers_present(self):
        client = TestClient(_make_app())
        resp = client.get("/ping")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Permissions-Policy"] == (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )
        assert resp.headers["Strict-Transport-Security"] == ("max-age=63072000; includeSubDomains")

    def test_handler_set_csp_is_not_overwritten(self):
        client = TestClient(_make_app())
        resp = client.get("/preset")
        assert resp.headers["Content-Security-Policy"] == "default-src 'none'"


class TestAppCsp:
    def test_default_src_self(self):
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp

    def test_script_src_allows_telegram_widget_only(self):
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "script-src 'self' https://telegram.org" in csp
        assert "cdn.jsdelivr.net" not in csp

    def test_style_src_attr_carries_unsafe_inline_not_style_src(self):
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "style-src-attr 'unsafe-inline'" in csp
        assert "style-src 'self';" in csp

    def test_img_src_allows_any_https_and_data_for_third_party_covers(self):
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "img-src 'self' https: data:" in csp

    def test_font_src_self_only(self):
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "font-src 'self'" in csp

    def test_connect_src_self_only(self):
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "connect-src 'self'" in csp

    def test_frame_src_allows_telegram_oauth_embed(self):
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "frame-src https://oauth.telegram.org" in csp

    def test_hardening_directives_present(self):
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "object-src 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "form-action 'self'" in csp

    def test_frame_ancestors_default(self):
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org" in csp

    def test_frame_ancestors_env_override(self, monkeypatch):
        monkeypatch.setenv("CSP_FRAME_ANCESTORS", "'none'")
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp


class TestConnectSrcExtra:
    def test_default_connect_src_is_self_only(self, monkeypatch):
        monkeypatch.delenv("CSP_CONNECT_SRC_EXTRA", raising=False)
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "connect-src 'self';" in csp

    def test_env_override_appends_external_collector_origin(self, monkeypatch):
        monkeypatch.setenv("CSP_CONNECT_SRC_EXTRA", "https://errors.example.com")
        client = TestClient(_make_app())
        csp = client.get("/ping").headers["Content-Security-Policy"]
        assert "connect-src 'self' https://errors.example.com;" in csp

    def test_docs_csp_is_unaffected_by_connect_src_extra(self, monkeypatch):
        monkeypatch.setenv("CSP_CONNECT_SRC_EXTRA", "https://errors.example.com")
        client = TestClient(_make_app())
        csp = client.get("/docs").headers["Content-Security-Policy"]
        assert "connect-src 'self';" in csp
        assert "errors.example.com" not in csp


class TestDocsCsp:
    def test_docs_path_gets_docs_csp(self):
        client = TestClient(_make_app())
        csp = client.get("/docs").headers["Content-Security-Policy"]
        assert "https://cdn.jsdelivr.net" in csp
        assert "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'" in csp

    def test_docs_oauth2_redirect_subpath_gets_docs_csp(self):
        client = TestClient(_make_app())
        csp = client.get("/docs/oauth2-redirect").headers["Content-Security-Policy"]
        assert "https://cdn.jsdelivr.net" in csp

    def test_redoc_path_gets_docs_csp_with_google_fonts(self):
        client = TestClient(_make_app())
        csp = client.get("/redoc").headers["Content-Security-Policy"]
        assert "https://fonts.googleapis.com" in csp
        assert "https://fonts.gstatic.com" in csp
        assert (
            "style-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'"
            in csp
        )

    def test_openapi_json_path_falls_through_to_app_csp(self):
        """A plain JSON endpoint gets no benefit from the docs UI's relaxed
        policy and should not carry it -- see the rationale comment above
        _DOCS_CSP_PATH_PREFIXES. It does carry the app policy's narrow
        style-src-attr 'unsafe-inline' (harmless for a JSON response, but
        that is the shared _app_csp() policy, not something openapi.json
        needs specifically)."""
        client = TestClient(_make_app())
        csp = client.get("/openapi.json").headers["Content-Security-Policy"]
        assert "jsdelivr" not in csp
        assert "script-src 'self' https://telegram.org;" in csp
        assert "default-src 'self'" in csp

    def test_docs_csp_still_carries_frame_ancestors(self):
        client = TestClient(_make_app())
        csp = client.get("/docs").headers["Content-Security-Policy"]
        assert "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org" in csp

    def test_non_docs_paths_never_get_jsdelivr(self):
        client = TestClient(_make_app())
        for path in ("/ping", "/openapi.json"):
            csp = client.get(path).headers["Content-Security-Policy"]
            assert "jsdelivr" not in csp, f"{path} unexpectedly carries the docs CSP"
