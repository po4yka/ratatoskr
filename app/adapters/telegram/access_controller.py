"""Access control for Telegram bot messages."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.application.services.user_interaction_service import async_safe_update_user_interaction

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.application.ports.users import UserRepositoryPort
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)

# Maximum number of distinct unauthorized user IDs tracked in-memory.
# Oldest entries are evicted when the cap is reached (LRU by insertion order).
_MAX_TRACKED_UIDS = 10_000


class _NullUserRepository:
    async def async_update_user_interaction(self, **_kwargs: object) -> None:
        return None


class AccessController:
    """Handles access control and user validation."""

    def __init__(
        self,
        cfg: AppConfig,
        db: Database | None,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict], None],
        user_repo: UserRepositoryPort | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.user_repo = user_repo or _NullUserRepository()
        self.response_formatter = response_formatter
        self._audit = audit_func

        if not self.cfg.telegram.allowed_user_ids:
            msg = "Telegram access control requires ALLOWED_USER_IDS to be configured."
            raise RuntimeError(msg)

        # Security tracking — OrderedDict preserves insertion order for LRU eviction.
        self._failed_attempts: OrderedDict[int, int] = OrderedDict()
        self._last_attempt_time: OrderedDict[int, float] = OrderedDict()
        self._block_notified_until: OrderedDict[int, float] = OrderedDict()
        self._deny_notified_until: OrderedDict[int, float] = OrderedDict()
        self.MAX_FAILED_ATTEMPTS = 3
        self.BLOCK_DURATION_SECONDS = 300  # 5 minutes
        self.DENY_NOTIFICATION_COOLDOWN_SECONDS = 300

    def _clear_tracking(self, uid: int) -> None:
        """Remove all in-memory tracking state for a user."""
        self._failed_attempts.pop(uid, None)
        self._last_attempt_time.pop(uid, None)
        self._block_notified_until.pop(uid, None)
        self._deny_notified_until.pop(uid, None)

    def _evict_lru(self) -> None:
        """Evict the oldest tracked UIDs when the cap is exceeded.

        Uses insertion order of each OrderedDict as a proxy for access recency.
        Only the oldest entries (across the union of all dicts) are dropped.
        Called after stale cleanup so that genuinely active UIDs are not evicted first.
        """
        tracked_uids = (
            set(self._failed_attempts)
            | set(self._last_attempt_time)
            | set(self._block_notified_until)
            | set(self._deny_notified_until)
        )
        overflow = len(tracked_uids) - _MAX_TRACKED_UIDS
        if overflow <= 0:
            return

        # Walk insertion order of _last_attempt_time (widest dict) to pick victims.
        # Fall back to arbitrary ordering for UIDs absent from that dict.
        ordered = list(self._last_attempt_time.keys())
        extras = [uid for uid in tracked_uids if uid not in self._last_attempt_time]
        for uid in ordered[:overflow] + extras[: max(0, overflow - len(ordered))]:
            self._clear_tracking(uid)

        logger.warning(
            "access_controller_lru_eviction",
            extra={"evicted": overflow, "cap": _MAX_TRACKED_UIDS},
        )

    def _cleanup_stale_tracking(self, current_time: float) -> int:
        """Reclaim stale unauthorized-user tracking state."""
        tracked_uids = (
            set(self._failed_attempts)
            | set(self._last_attempt_time)
            | set(self._block_notified_until)
            | set(self._deny_notified_until)
        )
        cleaned = 0

        for uid in tracked_uids:
            last_attempt_time = self._last_attempt_time.get(uid, 0.0)
            retention_deadline = max(
                last_attempt_time + self.BLOCK_DURATION_SECONDS if last_attempt_time else 0.0,
                self._block_notified_until.get(uid, 0.0),
                self._deny_notified_until.get(uid, 0.0),
            )
            if retention_deadline and current_time < retention_deadline:
                continue
            self._clear_tracking(uid)
            cleaned += 1

        return cleaned

    async def check_access(
        self, uid: int, message: Any, correlation_id: str, interaction_id: int, start_time: float
    ) -> bool:
        """Check if user has access to the bot."""
        allowed_ids = self.cfg.telegram.allowed_user_ids

        current_time = time.time()
        self._cleanup_stale_tracking(current_time)
        self._evict_lru()

        if uid in allowed_ids:
            # Reset failed attempts on successful access
            self._clear_tracking(uid)
            logger.info("access_granted", extra={"uid": uid})
            return True

        failed_count = self._failed_attempts.get(uid, 0)

        # Check if user is blocked due to too many failed attempts (unauthorized users only)
        if failed_count >= self.MAX_FAILED_ATTEMPTS:
            last_attempt_time = self._last_attempt_time.get(uid)
            time_since_last_attempt = (
                current_time - last_attempt_time
                if last_attempt_time is not None
                else self.BLOCK_DURATION_SECONDS
            )

            if time_since_last_attempt < self.BLOCK_DURATION_SECONDS:
                logger.warning(
                    "access_blocked_rate_limited",
                    extra={
                        "uid": uid,
                        "time_remaining": self.BLOCK_DURATION_SECONDS - time_since_last_attempt,
                        "failed_attempts": failed_count,
                    },
                )
                await self._maybe_notify_blocked(
                    uid, message, current_time, correlation_id=correlation_id
                )
                return False

            # Block window expired - reset counters so the user gets a fresh set of attempts
            self._clear_tracking(uid)
            failed_count = 0

        # Track failed attempts
        failed_count += 1
        self._failed_attempts[uid] = failed_count
        self._last_attempt_time[uid] = current_time

        logger.warning(
            "access_denied_list_mismatch",
            extra={
                "uid": uid,
                "allowed_count": len(allowed_ids),
                "failed_attempts": failed_count,
                "max_attempts": self.MAX_FAILED_ATTEMPTS,
            },
        )

        # Block user after too many failed attempts
        if failed_count >= self.MAX_FAILED_ATTEMPTS:
            logger.warning(
                "access_blocked_too_many_attempts",
                extra={
                    "uid": uid,
                    "failed_attempts": failed_count,
                    "block_duration_seconds": self.BLOCK_DURATION_SECONDS,
                },
            )
            await self._maybe_notify_blocked(
                uid,
                message,
                current_time,
                correlation_id=correlation_id,
                force=True,
                message_text=(
                    f"Access blocked after {failed_count} failed attempts. "
                    f"Try again in {self.BLOCK_DURATION_SECONDS // 60} minutes."
                ),
            )
            return False

        try:
            self._audit("WARN", "access_denied", {"uid": uid, "cid": correlation_id})
        except Exception:
            logger.warning("audit_callback_failed", extra={"uid": uid, "cid": correlation_id})

        if await self._maybe_notify_denied(
            uid, message, current_time, correlation_id=correlation_id
        ):
            logger.info("access_denied", extra={"uid": uid, "cid": correlation_id})

        if interaction_id:
            await async_safe_update_user_interaction(
                self.user_repo,
                interaction_id=interaction_id,
                response_sent=True,
                response_type="error",
                error_occurred=True,
                error_message="Access denied",
                start_time=start_time,
                logger_=logger,
            )
        return False

    async def _maybe_notify_blocked(
        self,
        uid: int,
        message: Any,
        current_time: float,
        *,
        correlation_id: str,
        force: bool = False,
        message_text: str | None = None,
    ) -> None:
        """Send block notification at most once per block window."""
        deadline = self._block_notified_until.get(uid, 0.0)
        if force or current_time >= deadline:
            try:
                await self.response_formatter.send_error_notification(
                    message, "access_blocked", correlation_id, details=message_text
                )
            except Exception:
                logger.warning(
                    "block_notification_failed", extra={"uid": uid, "cid": correlation_id}
                )
            self._block_notified_until[uid] = current_time + self.BLOCK_DURATION_SECONDS

    async def _maybe_notify_denied(
        self, uid: int, message: Any, current_time: float, *, correlation_id: str
    ) -> bool:
        """Send access denied notification with cooldown."""
        deadline = self._deny_notified_until.get(uid, 0.0)
        if current_time < deadline:
            return False

        try:
            await self.response_formatter.send_error_notification(
                message, "access_denied", correlation_id, details=str(uid)
            )
        except Exception:
            logger.warning("deny_notification_failed", extra={"uid": uid, "cid": correlation_id})
        self._deny_notified_until[uid] = current_time + self.DENY_NOTIFICATION_COOLDOWN_SECONDS
        return True
