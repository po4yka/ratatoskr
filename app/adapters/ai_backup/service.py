"""Orchestration for a single AI account backup run.

``AiBackupOrchestrationService`` owns the lifecycle of one ``(user_id, service)``
run: load the saved session, open an authenticated CloakBrowser context, drive
the provider client, persist the refreshed session, and record the outcome.

Notifications/health-pings are delegated to an injected ``BackupNotifier`` so
this adapter never imports the ``di``/``tasks`` tiers (which build the Telegram
bot client). The concrete notifier lives in ``app.tasks.ai_backup_sync``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from app.adapters.ai_backup.errors import (
    AiBackupAuthExpiredError,
    AiBackupMaxRequestsError,
    classify_error,
)
from app.core.logging_utils import get_logger
from app.db.models.ai_backup import AiBackupAuthorizationStatus
from app.security.secret_crypto import InvalidEncryptedSecretError

if TYPE_CHECKING:
    from app.adapters.ai_backup.repository import AiBackupRepository
    from app.adapters.ai_backup.session_store import AiBackupSessionStore
    from app.config import AppConfig
    from app.db.models.ai_backup import AiBackupService

logger = get_logger(__name__)


class BackupNotifier(Protocol):
    """Lifecycle hooks for a backup run (Telegram + healthcheck)."""

    async def on_start(self, service: AiBackupService) -> None: ...
    async def on_success(
        self, service: AiBackupService, counts: dict[str, int], correlation_id: str
    ) -> None: ...
    async def on_failure(self, service: AiBackupService, correlation_id: str) -> None: ...
    async def on_auth_expired(self, service: AiBackupService, correlation_id: str) -> None: ...


class NullNotifier:
    """No-op notifier (default; used in tests and when notifications are off)."""

    async def on_start(self, service: AiBackupService) -> None:
        return None

    async def on_success(
        self, service: AiBackupService, counts: dict[str, int], correlation_id: str
    ) -> None:
        return None

    async def on_failure(self, service: AiBackupService, correlation_id: str) -> None:
        return None

    async def on_auth_expired(self, service: AiBackupService, correlation_id: str) -> None:
        return None


class AiBackupOrchestrationService:
    """Runs one full backup for a single service."""

    def __init__(
        self,
        cfg: AppConfig,
        repo: AiBackupRepository,
        session_store: AiBackupSessionStore,
        notifier: BackupNotifier | None = None,
    ) -> None:
        self._cfg = cfg
        self._repo = repo
        self._session_store = session_store
        self._notifier: BackupNotifier = notifier or NullNotifier()

    async def run(self, user_id: int, service: AiBackupService) -> None:
        from app.adapters.ai_backup.chatgpt_client import ChatGptClient
        from app.adapters.ai_backup.client_factory import build_client
        from app.adapters.ai_backup.disk_writer import AiBackupDiskWriter
        from app.adapters.ai_backup.session_store import domain_for_service
        from app.adapters.content.browser_auth.authenticated_context import (
            PlaywrightAuthedFetcher,
            authenticated_context,
        )

        correlation_id = str(uuid.uuid4())
        row = await self._repo.ensure(user_id, service)

        if row.authorization_status == AiBackupAuthorizationStatus.EXPIRED:
            logger.info("ai_backup_auth_expired_halted", extra={"service": service.value})
            return

        if row.backoff_until and dt.datetime.now(tz=dt.UTC) < row.backoff_until:
            logger.info(
                "ai_backup_backoff_active",
                extra={"service": service.value, "until": row.backoff_until.isoformat()},
            )
            return

        ai_cfg = self._cfg.ai_backup
        writer: AiBackupDiskWriter | None = None
        refreshed_out: list[dict] = []
        counts: dict[str, int] = {}
        requests_made = 0
        skipped = 0
        fetcher: PlaywrightAuthedFetcher | None = None

        try:
            session_snapshot = await self._session_store.load_for_refresh(user_id, service)
            if session_snapshot is None:
                await self._repo.mark_authorization_missing(user_id, service)
                logger.info("ai_backup_no_session", extra={"service": service.value})
                return
            storage_state = session_snapshot.storage_state

            writer = AiBackupDiskWriter(
                Path(ai_cfg.data_path),
                service.value,
                dt.datetime.now(tz=dt.UTC).date(),
                correlation_id,
                min_free_bytes=ai_cfg.min_free_bytes,
            )
            await self._notifier.on_start(service)
            async with authenticated_context(
                domain_for_service(service),
                storage_state,
                endpoint_url=self._cfg.scraper.cloakbrowser_url,
                refreshed_out=refreshed_out,
            ) as (_page, ctx):
                fetcher = PlaywrightAuthedFetcher(
                    ctx,
                    host_allowlist=list(ai_cfg.host_allowlist),
                    inter_request_delay_sec=ai_cfg.request_delay_ms / 1000.0,
                    max_requests=ai_cfg.max_requests_per_run,
                    max_response_bytes=ai_cfg.max_response_bytes,
                    max_run_bytes=ai_cfg.max_run_bytes,
                )
                client = build_client(
                    service, fetcher, writer, ai_cfg, last_backed_up_at=row.last_backed_up_at
                )
                if isinstance(client, ChatGptClient):
                    await client.exchange_session_cookie()
                counts = await client.collect()
                skipped = client.skipped
                requests_made = fetcher.requests_made

            writer.finalize_manifest(
                counts=counts,
                requests_made=requests_made,
                skipped_incremental=skipped,
                incremental=ai_cfg.incremental,
            )
            await self._repo.record_success(
                user_id, service, counts=counts, backup_path=str(writer.run_dir)
            )
            await self._notifier.on_success(service, counts, correlation_id)
        except InvalidEncryptedSecretError:
            message = "Stored AI backup session cannot be decrypted; re-ingest is required"
            await self._repo.mark_auth_expired(user_id, service, message)
            await self._notifier.on_auth_expired(service, correlation_id)
            logger.warning(
                "ai_backup_session_decrypt_action_required",
                extra={"service": service.value, "correlation_id": correlation_id},
            )
            return
        except AiBackupAuthExpiredError as exc:
            await self._repo.mark_auth_expired(user_id, service, str(exc))
            await self._notifier.on_auth_expired(service, correlation_id)
            logger.warning(
                "ai_backup_auth_expired",
                extra={"service": service.value, "correlation_id": correlation_id},
            )
            return
        except AiBackupMaxRequestsError as exc:
            # Rate-limited (or per-run cap hit) mid-sweep. Conversations already
            # written stay on disk; the next run resumes from them
            # (load_saved_conversation) and fetches only what is missing, so the
            # backup converges instead of re-fetching everything and re-tripping
            # the limit. Persist a partial manifest so progress is recorded, then
            # mark a retryable failure (the scheduler retries after backoff).
            if writer is not None:
                try:
                    writer.finalize_manifest(
                        counts=writer.partial_counts(),
                        requests_made=fetcher.requests_made if fetcher is not None else 0,
                        skipped_incremental=0,
                        incremental=ai_cfg.incremental,
                    )
                except Exception:
                    logger.warning(
                        "ai_backup_partial_manifest_failed",
                        extra={"service": service.value, "correlation_id": correlation_id},
                    )
            await self._repo.record_failure(
                user_id, service, category=classify_error(exc), message=str(exc)
            )
            await self._notifier.on_failure(service, correlation_id)
            logger.warning(
                "ai_backup_rate_limited_partial",
                extra={"service": service.value, "correlation_id": correlation_id},
            )
            raise
        except Exception as exc:
            await self._repo.record_failure(
                user_id, service, category=classify_error(exc), message=str(exc)
            )
            await self._notifier.on_failure(service, correlation_id)
            raise
        finally:
            # Persist rotated cookies only while the exact session loaded by this
            # run is still current. A revoke or replacement wins over stale work.
            if refreshed_out:
                try:
                    refreshed = await self._session_store.refresh(
                        user_id,
                        service,
                        refreshed_out[0],
                        expected_revision=session_snapshot.revision,
                    )
                    if not refreshed:
                        logger.info(
                            "ai_backup_session_refresh_skipped_stale",
                            extra={"service": service.value},
                        )
                except Exception:
                    logger.warning(
                        "ai_backup_session_refresh_save_failed",
                        extra={"service": service.value},
                    )

        assert writer is not None
        logger.info(
            "ai_backup_run_complete",
            extra={
                "service": service.value,
                "correlation_id": correlation_id,
                "counts": counts,
                "skipped": skipped,
                "path": str(writer.run_dir),
            },
        )


__all__ = ["AiBackupOrchestrationService", "BackupNotifier", "NullNotifier"]
