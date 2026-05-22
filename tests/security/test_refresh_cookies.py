from __future__ import annotations

from starlette.responses import Response

from app.api.routers.auth.cookies import clear_refresh_cookie, set_refresh_cookie


def _set_cookie_header(response: Response) -> str:
    [header] = response.headers.getlist("set-cookie")
    return header


def _cookie_attributes(header: str) -> dict[str, str | bool]:
    parts = [part.strip() for part in header.split(";")]
    attrs: dict[str, str | bool] = {"value": parts[0].split("=", 1)[1]}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            attrs[key.lower()] = value
        else:
            attrs[part.lower()] = True
    return attrs


def test_refresh_cookie_persistent_attributes_are_exact() -> None:
    response = Response()

    set_refresh_cookie(response, "refresh-token")

    assert _set_cookie_header(response) == "ratatoskr_refresh_token=refresh-token; HttpOnly; Max-Age=2592000; Path=/v1/auth; SameSite=strict; Secure"


def test_refresh_cookie_session_attributes_are_exact() -> None:
    response = Response()

    set_refresh_cookie(response, "refresh-token", max_age=None)

    assert _set_cookie_header(response) == "ratatoskr_refresh_token=refresh-token; HttpOnly; Path=/v1/auth; SameSite=strict; Secure"


def test_refresh_cookie_clear_matches_set_cookie_security_attributes() -> None:
    set_response = Response()
    clear_response = Response()

    set_refresh_cookie(set_response, "refresh-token")
    clear_refresh_cookie(clear_response)

    set_attrs = _cookie_attributes(_set_cookie_header(set_response))
    clear_attrs = _cookie_attributes(_set_cookie_header(clear_response))
    for attr in ("path", "samesite", "httponly", "secure"):
        assert clear_attrs[attr] == set_attrs[attr]
    assert clear_attrs["value"] == '""'
    assert clear_attrs["max-age"] == "0"
    assert "expires" in clear_attrs
