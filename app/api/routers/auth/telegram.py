"""
Telegram authentication HMAC verification.
"""

import hashlib
import hmac
import time

from app.api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
)
from app.config import Config
from app.core.logging_utils import get_logger

logger = get_logger(__name__)


def verify_telegram_auth(
    user_id: int,
    auth_hash: str,
    auth_date: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    photo_url: str | None = None,
) -> bool:
    """
    Verify Telegram authentication hash.

    Implements the verification algorithm from:
    https://core.telegram.org/widgets/login#checking-authorization

    Args:
        user_id: Telegram user ID
        auth_hash: Authentication hash from Telegram
        auth_date: Timestamp when auth was created
        username: Optional Telegram username
        first_name: Optional first name
        last_name: Optional last name
        photo_url: Optional profile photo URL

    Returns:
        True if authentication is valid

    Raises:
        AuthenticationError: If authentication fails
        AuthorizationError: If user not in whitelist
        ConfigurationError: If BOT_TOKEN not configured
    """
    # Check timestamp freshness (15 minute window)
    current_time = int(time.time())
    age_seconds = current_time - auth_date

    if age_seconds > 900:  # 15 minutes
        logger.warning(
            f"Telegram auth expired for user {user_id}. Age: {age_seconds}s",
            extra={"user_id": user_id, "age_seconds": age_seconds},
        )
        raise AuthenticationError("Authentication data has expired. Please log in again.")

    if age_seconds < -60:  # Allow 1 minute clock skew
        logger.warning(
            f"Telegram auth timestamp in future for user {user_id}. Skew: {-age_seconds}s",
            extra={"user_id": user_id, "skew_seconds": -age_seconds},
        )
        raise AuthenticationError("Authentication timestamp is in the future. Check device clock.")

    # Build data check string according to Telegram spec
    data_check_arr = [f"auth_date={auth_date}", f"id={user_id}"]

    if first_name:
        data_check_arr.append(f"first_name={first_name}")
    if last_name:
        data_check_arr.append(f"last_name={last_name}")
    if photo_url:
        data_check_arr.append(f"photo_url={photo_url}")
    if username:
        data_check_arr.append(f"username={username}")

    # Sort alphabetically (required by Telegram)
    data_check_arr.sort()
    data_check_string = "\n".join(data_check_arr)

    # Get bot token
    try:
        bot_token = Config.get("BOT_TOKEN")
    except ValueError as err:
        logger.error("Telegram auth credential is not configured - cannot verify request")
        raise ConfigurationError(
            "Server misconfiguration: BOT_TOKEN is not set.", config_key="BOT_TOKEN"
        ) from err

    if not bot_token:
        logger.error("Telegram auth credential is empty - cannot verify request")
        raise ConfigurationError(
            "Server misconfiguration: BOT_TOKEN is empty.", config_key="BOT_TOKEN"
        )

    # Compute secret key: SHA256(bot_token)
    secret_key = hashlib.sha256(bot_token.encode()).digest()

    # Compute HMAC-SHA256
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    # Verify hash matches using constant-time comparison
    if not hmac.compare_digest(computed_hash, auth_hash):
        logger.warning(
            f"Invalid Telegram auth hash for user {user_id}",
            extra={"user_id": user_id, "username": username},
        )
        raise AuthenticationError("Invalid authentication hash. Please try logging in again.")

    if not Config.is_user_allowed(user_id, fail_open_when_empty=False):
        logger.warning(
            f"User {user_id} not in whitelist",
            extra={"user_id": user_id, "username": username},
        )
        raise AuthorizationError("User not authorized. Contact administrator to request access.")

    logger.info(
        f"Telegram auth verified for user {user_id}",
        extra={"user_id": user_id, "username": username},
    )

    return True
