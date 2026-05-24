"""Push notification delivery via Firebase Admin SDK (FCM/APNS).

Gracefully degrades to a no-op when ``firebase-admin`` is not installed or
when the feature is disabled via configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.config.push import PushNotificationConfig
    from app.infrastructure.persistence.repositories.device_repository import (
        DeviceRepositoryAdapter,
    )

logger = get_logger(__name__)

# Attempt to import firebase_admin; fall back gracefully.
try:
    import firebase_admin
    from firebase_admin import credentials, messaging

    _FIREBASE_AVAILABLE = True
except ImportError:
    _FIREBASE_AVAILABLE = False
    firebase_admin = None
    credentials = None
    messaging = None


class PushNotificationService:
    """Send push notifications to user devices via FCM/APNS.

    When ``firebase-admin`` is unavailable or push notifications are disabled,
    all public methods silently no-op so callers need not check availability.
    """

    def __init__(
        self,
        config: PushNotificationConfig,
        device_repository: DeviceRepositoryAdapter,
    ) -> None:
        self._config = config
        self._device_repo = device_repository
        self._initialized = False
        self._app: Any = None
        self._background_tasks: set[Any] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Initialize the Firebase Admin SDK from the configured credentials.

        Must be called once at application startup.  Safe to call multiple
        times -- subsequent calls are no-ops.
        """
        if self._initialized:
            return

        if not self._config.enabled:
            logger.info("push_notifications_disabled")
            return

        if not _FIREBASE_AVAILABLE:
            logger.warning(
                "push_notifications_unavailable",
                extra={"reason": "firebase-admin package not installed"},
            )
            return

        cred_path = self._config.firebase_credentials_path
        if not cred_path:
            logger.warning(
                "push_notifications_unavailable",
                extra={"reason": "FIREBASE_CREDENTIALS_PATH not set"},
            )
            return

        try:
            cred = credentials.Certificate(cred_path)
            self._app = firebase_admin.initialize_app(cred)
            self._initialized = True
            logger.info(
                "push_notifications_initialized",
                extra={"credentials_path": cred_path},
            )
        except Exception as exc:
            logger.exception(
                "push_notifications_init_failed",
                extra={"credentials_path": cred_path, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_to_user(
        self,
        user_id: int,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> None:
        """Send a push notification to all active devices of a user.

        Args:
            user_id: Telegram user ID whose devices should receive the push.
            title: Notification title.
            body: Notification body text.
            data: Optional key/value payload forwarded to the client app.
        """
        if not self._initialized:
            return

        devices = await self._device_repo.async_list_user_devices(user_id, active_only=True)
        if not devices:
            logger.debug(
                "push_no_devices_for_user",
                extra={"user_id": user_id},
            )
            return

        for device in devices:
            token = device.get("token")
            platform = device.get("platform", "android")
            if not token:
                continue
            await self.send_to_device(
                token=token,
                platform=platform,
                title=title,
                body=body,
                data=data,
            )

    async def send_to_device(
        self,
        token: str,
        platform: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> None:
        """Send a push notification to a single device.

        Args:
            token: FCM/APNS device token.
            platform: ``"ios"`` or ``"android"``.
            title: Notification title.
            body: Notification body text.
            data: Optional key/value payload forwarded to the client app.
        """
        if not self._initialized:
            return

        try:
            message = self._build_message(
                token=token,
                platform=platform,
                title=title,
                body=body,
                data=data,
            )
            # firebase_admin.messaging.send is synchronous; run off the event loop.
            import asyncio

            response = await asyncio.to_thread(messaging.send, message)
            logger.info(
                "push_notification_sent",
                extra={
                    "platform": platform,
                    "response": response,
                },
            )
        except Exception as exc:
            # Catch all Firebase errors (and any other unexpected exceptions)
            # so a single failed device never blocks the rest.
            logger.warning(
                "push_notification_send_failed",
                extra={
                    "platform": platform,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            # Deactivate token on unregistered/invalid errors
            self._handle_token_error(exc, token)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_message(
        *,
        token: str,
        platform: str,
        title: str,
        body: str,
        data: dict[str, str] | None,
    ) -> Any:
        """Construct a ``firebase_admin.messaging.Message``."""
        notification = messaging.Notification(title=title, body=body)
        kwargs: dict[str, Any] = {
            "token": token,
            "notification": notification,
            "data": data or {},
        }

        if platform == "ios":
            kwargs["apns"] = messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        badge=1,
                    ),
                ),
            )
        elif platform == "android":
            kwargs["android"] = messaging.AndroidConfig(
                notification=messaging.AndroidNotification(sound="default"),
            )

        return messaging.Message(**kwargs)

    @staticmethod
    def _invalid_token_error_types() -> tuple[type[BaseException], ...]:
        exceptions_module = getattr(firebase_admin, "exceptions", None)
        if exceptions_module is None:
            from firebase_admin import exceptions as imported_exceptions_module

            exceptions_module = imported_exceptions_module

        return (
            exceptions_module.NotFoundError,
            exceptions_module.InvalidArgumentError,
        )

    def _handle_token_error(self, exc: Exception, token: str) -> None:
        """Deactivate a device token when Firebase reports it as invalid."""
        if not _FIREBASE_AVAILABLE:
            return

        if isinstance(exc, self._invalid_token_error_types()):
            logger.info(
                "push_deactivating_invalid_token",
                extra={"token_prefix": token[:8] + "..."},
            )
            # Fire-and-forget deactivation; errors logged by the repository.
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._device_repo.async_deactivate_device(token))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except RuntimeError:
                logger.debug("push_device_deactivate_no_event_loop", extra={"token": token[:8]})


def create_push_notification_service(
    config: PushNotificationConfig,
    device_repository: DeviceRepositoryAdapter,
) -> PushNotificationService:
    """Factory: create and initialize a ``PushNotificationService``."""
    service = PushNotificationService(config, device_repository)
    service.initialize()
    return service
