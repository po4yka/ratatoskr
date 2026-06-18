from __future__ import annotations

from app.di.repositories import build_audit_log_repository
from app.di.telegram import build_telegram_runtime

RUNTIME_BUILDER = build_telegram_runtime
AUDIT_REPOSITORY_BUILDER = build_audit_log_repository
