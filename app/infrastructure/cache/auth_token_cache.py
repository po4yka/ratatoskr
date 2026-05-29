"""Redis cache for authentication tokens.

Provides O(1) lookup for refresh token validation instead of DB queries.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.infrastructure.cache.redis_cache import RedisCache

logger = get_logger(__name__)

# TTL for a revocation tombstone written when a token is revoked but was never
# previously cached.  Short enough to avoid Redis pollution, long enough to
# cover any refresh-token lifetime race window.  A tombstone written here
# prevents a later set_token call from re-caching the revoked hash as valid.
_REVOCATION_TOMBSTONE_TTL_SECONDS = 3_600  # 1 hour


class AuthTokenCache:
    """Cache refresh tokens in Redis for fast validation.

    Key pattern: ratatoskr:auth:token:{token_hash}
    Value: {"user_id": int, "client_id": str | None, "expires_at": str, "is_revoked": bool}
    TTL: Aligned with token expiry (configurable via REDIS_AUTH_TOKEN_CACHE_TTL_SECONDS)

    Fallback: On cache miss, query PostgreSQL through the auth repository.
    """

    def __init__(self, cache: RedisCache, cfg: AppConfig) -> None:
        self._cache = cache
        self._cfg = cfg

    @property
    def enabled(self) -> bool:
        return self._cache.enabled

    async def get_token(self, token_hash: str) -> dict[str, Any] | None:
        """Get cached token data by hash.

        Returns:
            Token data dict or None if not cached.
        """
        if not self._cache.enabled:
            return None

        cached = await self._cache.get_json("auth", "token", token_hash)
        if not isinstance(cached, dict):
            logger.debug(
                "auth_token_cache_miss",
                extra={"token_hash_prefix": token_hash[:8]},
            )
            return None

        logger.debug(
            "auth_token_cache_hit",
            extra={"token_hash_prefix": token_hash[:8]},
        )
        return cached

    async def set_token(
        self,
        token_hash: str,
        *,
        user_id: int,
        client_id: str | None,
        expires_at: datetime | str,
        is_revoked: bool = False,
        token_id: int | None = None,
    ) -> bool:
        """Cache token data.

        Args:
            token_hash: SHA256 hash of the refresh token.
            user_id: Associated user ID.
            client_id: Client application identifier.
            expires_at: Token expiration time.
            is_revoked: Whether the token is revoked.
            token_id: Database ID of the token record.

        Returns:
            True if cached successfully, False otherwise.
        """
        if not self._cache.enabled:
            return False

        # Format expires_at as ISO string if it's a datetime
        expires_at_str = (
            expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at)
        )

        value = {
            "user_id": user_id,
            "client_id": client_id,
            "expires_at": expires_at_str,
            "is_revoked": is_revoked,
        }
        if token_id is not None:
            value["id"] = token_id

        ttl = self._cfg.redis.auth_token_cache_ttl_seconds
        success = await self._cache.set_json(
            value=value,
            ttl_seconds=ttl,
            parts=("auth", "token", token_hash),
        )

        if success:
            logger.debug(
                "auth_token_cached",
                extra={"token_hash_prefix": token_hash[:8], "ttl": ttl},
            )
        return success

    async def invalidate_token(self, token_hash: str) -> bool:
        """Invalidate (delete) a cached token.

        Used when a token is revoked or deleted.

        Returns:
            True if the operation succeeded, False otherwise.
        """
        if not self._cache.enabled:
            return False

        client = await self._cache._get_client()
        if not client:
            return False

        from app.infrastructure.redis import redis_key

        key = redis_key(self._cfg.redis.prefix, "auth", "token", token_hash)
        try:
            await client.delete(key)
            logger.debug(
                "auth_token_cache_invalidated",
                extra={"token_hash_prefix": token_hash[:8]},
            )
            return True
        except Exception as exc:
            logger.warning(
                "auth_token_cache_invalidate_failed",
                exc_info=True,
                extra={"token_hash_prefix": token_hash[:8], "error": str(exc)},
            )
            return False

    async def mark_revoked(self, token_hash: str) -> bool:
        """Mark a token as revoked in cache, writing a tombstone if needed.

        If the token is already cached, update its ``is_revoked`` flag in-place
        using the configured auth-token TTL.

        If the token has never been cached (e.g. Redis was unavailable at
        creation time, or the cache was flushed), write a minimal revocation
        tombstone with a short TTL.  This prevents a subsequent
        ``async_get_refresh_token_by_hash`` call from re-populating the cache
        with ``is_revoked=False`` between the DB revocation and the next read.

        Returns:
            True if a cache entry was written, False on error.
        """
        if not self._cache.enabled:
            return False

        cached = await self.get_token(token_hash)
        if cached:
            cached["is_revoked"] = True
            ttl = self._cfg.redis.auth_token_cache_ttl_seconds
        else:
            # Token was never cached; write a tombstone so any future
            # validation attempt on this hash sees is_revoked=True from cache
            # rather than receiving a cache miss and potentially being served
            # a stale valid entry.
            cached = {"is_revoked": True}
            ttl = _REVOCATION_TOMBSTONE_TTL_SECONDS
            logger.debug(
                "auth_token_revocation_tombstone_written",
                extra={"token_hash_prefix": token_hash[:8], "ttl": ttl},
            )

        return await self._cache.set_json(
            value=cached,
            ttl_seconds=ttl,
            parts=("auth", "token", token_hash),
        )
