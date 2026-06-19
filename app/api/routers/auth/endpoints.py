"""
Authentication API endpoints.

Aggregates focused auth sub-routers (telegram, secret keys, me, sessions) into
the single router re-exported by `app.api.routers.auth`.
"""

from __future__ import annotations

from app.api.routers.auth._fastapi import APIRouter

from . import (
    endpoints_credentials,
    endpoints_me,
    endpoints_secret_keys,
    endpoints_sessions,
    endpoints_telegram,
    magic_link,
)

router = APIRouter()

# Aggregate routers (route paths are defined in each sub-router)
router.include_router(endpoints_telegram.router)
router.include_router(endpoints_secret_keys.router)
router.include_router(endpoints_credentials.router)
router.include_router(magic_link.router)
router.include_router(endpoints_me.router)
router.include_router(endpoints_sessions.router)

# Re-export handlers so tests can call them directly without going through HTTP.
telegram_login = endpoints_telegram.telegram_login
get_telegram_link_status = endpoints_telegram.get_telegram_link_status
begin_telegram_link = endpoints_telegram.begin_telegram_link
complete_telegram_link = endpoints_telegram.complete_telegram_link
unlink_telegram = endpoints_telegram.unlink_telegram

secret_login = endpoints_secret_keys.secret_login
create_secret_key = endpoints_secret_keys.create_secret_key
rotate_secret_key = endpoints_secret_keys.rotate_secret_key
revoke_secret_key = endpoints_secret_keys.revoke_secret_key
list_secret_keys = endpoints_secret_keys.list_secret_keys

get_current_user_info = endpoints_me.get_current_user_info
delete_account = endpoints_me.delete_account

credentials_login = endpoints_credentials.credentials_login
change_password = endpoints_credentials.change_password

request_magic_link = magic_link.request_magic_link
verify_magic_link = magic_link.verify_magic_link

refresh_access_token = endpoints_sessions.refresh_access_token
logout = endpoints_sessions.logout
logout_all = endpoints_sessions.logout_all
list_sessions = endpoints_sessions.list_sessions
