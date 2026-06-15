"""Comprehensive tests for rate limiter security module.

This test suite aims to achieve >80% coverage by testing:
- Edge cases (empty queues, zero limits)
- Branch conditions
- Error handling
- Concurrent operations
- Redis-backed limiter
- Time-based logic
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.security.rate_limiter import RateLimitConfig, RedisUserRateLimiter, UserRateLimiter


class TestUserRateLimiter(unittest.IsolatedAsyncioTestCase):
    """Test suite for UserRateLimiter."""

    async def test_basic_rate_limiting(self):
        """Test basic rate limiting functionality."""
        limiter = UserRateLimiter(
            RateLimitConfig(max_requests=3, window_seconds=1, max_concurrent=2)
        )

        user_id = 12345

        # First 3 requests should pass
        for i in range(3):
            allowed, _msg = await limiter.check_and_record(user_id, operation=f"request_{i}")
            assert allowed, f"Request {i} should be allowed"
            assert _msg is None

        # 4th request should be blocked
        allowed, _msg = await limiter.check_and_record(user_id, operation="request_4")
        assert not allowed
        assert _msg is not None
        assert "Rate limit exceeded" in _msg

    async def test_sliding_window(self):
        """Test sliding window behavior."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=2, window_seconds=1))

        user_id = 12345

        # Make 2 requests
        await limiter.check_and_record(user_id)
        await limiter.check_and_record(user_id)

        # 3rd should fail
        allowed, _ = await limiter.check_and_record(user_id)
        assert not allowed

        # Wait for window to expire
        await asyncio.sleep(1.1)

        # Should work again
        allowed, _ = await limiter.check_and_record(user_id)
        assert allowed

    async def test_default_cooldown_allows_after_window(self):
        """Ensure default cooldown duration matches the sliding window length."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=2, window_seconds=1))

        user_id = 54321

        # Hit the limit quickly
        await limiter.check_and_record(user_id)
        await limiter.check_and_record(user_id)

        allowed, message = await limiter.check_and_record(user_id)
        assert not allowed
        assert message is not None
        assert "Cooldown active for 1 seconds" in message

        # Wait just longer than the configured window/cooldown length
        await asyncio.sleep(1.05)

        allowed, message = await limiter.check_and_record(user_id)
        assert allowed
        assert message is None

    async def test_concurrent_operations(self):
        """Test concurrent operation limiting."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=10, max_concurrent=2))

        user_id = 12345

        # Acquire 2 slots
        assert await limiter.acquire_concurrent_slot(user_id)
        assert await limiter.acquire_concurrent_slot(user_id)

        # 3rd should fail
        assert not await limiter.acquire_concurrent_slot(user_id)

        # Release one slot
        await limiter.release_concurrent_slot(user_id)

        # Should work again
        assert await limiter.acquire_concurrent_slot(user_id)

    async def test_per_user_isolation(self):
        """Test that rate limits are isolated per user."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=2, window_seconds=10))

        user1 = 111
        user2 = 222

        # User 1 makes 2 requests
        await limiter.check_and_record(user1)
        await limiter.check_and_record(user1)

        # User 1 should be limited
        allowed, _ = await limiter.check_and_record(user1)
        assert not allowed

        # User 2 should not be affected
        allowed, _ = await limiter.check_and_record(user2)
        assert allowed

    async def test_cost_based_limiting(self):
        """Test cost-based rate limiting."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=5, window_seconds=10))

        user_id = 12345

        # Request with cost=3
        allowed, _ = await limiter.check_and_record(user_id, cost=3)
        assert allowed

        # Request with cost=2
        allowed, _ = await limiter.check_and_record(user_id, cost=2)
        assert allowed

        # Total is now 5, next request should fail
        allowed, _ = await limiter.check_and_record(user_id, cost=1)
        assert not allowed

    async def test_cleanup_expired(self):
        """Test cleanup of expired entries."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=5, window_seconds=1))

        # Create requests for multiple users
        await limiter.check_and_record(111)
        await limiter.check_and_record(222)
        await limiter.check_and_record(333)

        # Wait for window to expire
        await asyncio.sleep(1.1)

        # Cleanup should remove all users
        cleaned = await limiter.cleanup_expired()
        assert cleaned == 3

    async def test_cooldown_after_limit(self):
        """Test that cooldown is applied after exceeding limit."""
        limiter = UserRateLimiter(
            RateLimitConfig(max_requests=2, window_seconds=1, cooldown_multiplier=2.0)
        )

        user_id = 12345

        # Exhaust limit
        await limiter.check_and_record(user_id)
        await limiter.check_and_record(user_id)

        # Exceed limit
        allowed, _msg = await limiter.check_and_record(user_id)
        assert not allowed
        assert "Cooldown active" in _msg

        # Wait for window but not cooldown
        await asyncio.sleep(1.1)

        # Should still be in cooldown (2x window = 2 seconds)
        allowed, _msg = await limiter.check_and_record(user_id)
        assert not allowed
        assert "cooldown" in _msg.lower()

    # New tests for missing coverage

    async def test_rate_limit_with_empty_queue(self):
        """Test rate limit exceeded with empty request queue (line 103)."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=0, window_seconds=1))
        user_id = 12345

        # First request should fail (max_requests=0)
        allowed, _msg = await limiter.check_and_record(user_id)
        assert not allowed
        assert "Rate limit exceeded" in _msg
        # Queue is empty, so retry_after should be window_seconds

    async def test_concurrent_limit_check_during_rate_check(self):
        """Test concurrent limit checked in check_and_record (lines 130-143)."""
        limiter = UserRateLimiter(
            RateLimitConfig(max_requests=10, window_seconds=10, max_concurrent=1)
        )
        user_id = 12345

        # Acquire the only concurrent slot
        await limiter.acquire_concurrent_slot(user_id)

        # Try to record a request while at concurrent limit
        allowed, _msg = await limiter.check_and_record(user_id)
        assert not allowed
        assert "Too many concurrent operations" in _msg
        assert "Maximum: 1" in _msg

    async def test_release_concurrent_slot_to_zero(self):
        """Test release_concurrent_slot removes entry when count reaches 0."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=10, max_concurrent=3))
        user_id = 12345

        # Acquire then release
        await limiter.acquire_concurrent_slot(user_id)
        await limiter.release_concurrent_slot(user_id)

        # Verify removed from dict
        async with limiter._lock:
            assert user_id not in limiter._user_concurrent

    async def test_release_concurrent_slot_non_existent_user(self):
        """Test release_concurrent_slot for user with no slots."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=10, max_concurrent=3))
        user_id = 99999

        # Release for user who never acquired
        await limiter.release_concurrent_slot(user_id)

        # Should not crash
        async with limiter._lock:
            assert user_id not in limiter._user_concurrent

    async def test_cleanup_with_no_expired_users(self):
        """Test cleanup_expired returns 0 when nothing to clean."""
        limiter = UserRateLimiter(RateLimitConfig(max_requests=5, window_seconds=10))

        # No users added
        cleaned = await limiter.cleanup_expired()
        assert cleaned == 0

    async def test_default_config_values(self):
        """Test UserRateLimiter with default config."""
        limiter = UserRateLimiter()  # Uses default RateLimitConfig
        user_id = 12345

        # Default is max_requests=10, window_seconds=60
        for _ in range(10):
            allowed, _ = await limiter.check_and_record(user_id)
            assert allowed

        # 11th should fail
        allowed, _ = await limiter.check_and_record(user_id)
        assert not allowed


class TestRedisUserRateLimiter(unittest.IsolatedAsyncioTestCase):
    """Test suite for RedisUserRateLimiter."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_redis = MagicMock()
        self.config = RateLimitConfig(max_requests=5, window_seconds=10, max_concurrent=2)
        self.limiter = RedisUserRateLimiter(self.mock_redis, self.config, "test")

    async def test_check_and_record_allowed(self):
        """Test check_and_record allows request under limit."""
        # Mock Redis pipeline - must return a regular mock, not AsyncMock
        mock_pipe = MagicMock()
        mock_pipe.incrby = MagicMock()
        mock_pipe.expire = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[3, True])  # count=3, expire=True
        self.mock_redis.pipeline.return_value = mock_pipe

        allowed, _msg = await self.limiter.check_and_record(12345, cost=1)

        assert allowed
        assert _msg is None
        assert self.limiter.last_remaining == 2  # 5 - 3 = 2

    async def test_check_and_record_exceeded(self):
        """Test check_and_record blocks when limit exceeded."""
        # Mock Redis pipeline
        mock_pipe = MagicMock()
        mock_pipe.incrby = MagicMock()
        mock_pipe.expire = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[7, True])  # count=7 > max=5
        self.mock_redis.pipeline.return_value = mock_pipe

        allowed, _msg = await self.limiter.check_and_record(12345, cost=1)

        assert not allowed
        assert "Rate limit exceeded" in _msg
        assert "Cooldown active" in _msg

    async def test_acquire_concurrent_slot_success(self):
        """Test acquire_concurrent_slot succeeds under limit."""
        self.mock_redis.incr = AsyncMock(return_value=1)
        self.mock_redis.expire = AsyncMock()

        result = await self.limiter.acquire_concurrent_slot(12345)

        assert result is True
        self.mock_redis.incr.assert_called_once()
        self.mock_redis.expire.assert_called_once()

    async def test_acquire_concurrent_slot_at_limit(self):
        """Test acquire_concurrent_slot fails when limit exceeded."""
        self.mock_redis.incr = AsyncMock(return_value=3)  # > max_concurrent=2
        self.mock_redis.decr = AsyncMock()

        result = await self.limiter.acquire_concurrent_slot(12345)

        assert result is False
        self.mock_redis.decr.assert_called_once()  # Should decrement back

    async def test_acquire_concurrent_slot_no_expire_on_non_first(self):
        """Test acquire_concurrent_slot doesn't set expire if not first increment."""
        self.mock_redis.incr = AsyncMock(return_value=2)  # Not the first
        self.mock_redis.expire = AsyncMock()

        result = await self.limiter.acquire_concurrent_slot(12345)

        assert result is True
        self.mock_redis.expire.assert_not_called()

    async def test_release_concurrent_slot_positive_count(self):
        """Test release_concurrent_slot decrements but doesn't delete."""
        self.mock_redis.decr = AsyncMock(return_value=1)  # Still positive
        self.mock_redis.delete = AsyncMock()

        await self.limiter.release_concurrent_slot(12345)

        self.mock_redis.decr.assert_called_once()
        self.mock_redis.delete.assert_not_called()

    async def test_release_concurrent_slot_zero_count(self):
        """Test release_concurrent_slot deletes key when count reaches zero."""
        self.mock_redis.decr = AsyncMock(return_value=0)
        self.mock_redis.delete = AsyncMock()

        await self.limiter.release_concurrent_slot(12345)

        self.mock_redis.decr.assert_called_once()
        self.mock_redis.delete.assert_called_once()

    async def test_release_concurrent_slot_negative_count(self):
        """Test release_concurrent_slot deletes key on negative count."""
        self.mock_redis.decr = AsyncMock(return_value=-1)
        self.mock_redis.delete = AsyncMock()

        await self.limiter.release_concurrent_slot(12345)

        self.mock_redis.delete.assert_called_once()

    async def test_window_key_format(self):
        """Test _window_key generates correct format."""
        key = self.limiter._window_key(12345, 1000)
        assert key == "test:tg_rate:12345:1000"

    async def test_concurrency_key_format(self):
        """Test _concurrency_key generates correct format."""
        key = self.limiter._concurrency_key(12345)
        assert key == "test:tg_concurrent:12345"

    async def test_check_and_record_with_cost(self):
        """Test check_and_record with custom cost."""
        mock_pipe = MagicMock()
        mock_pipe.incrby = MagicMock()
        mock_pipe.expire = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[4, True])
        self.mock_redis.pipeline.return_value = mock_pipe

        allowed, _msg = await self.limiter.check_and_record(12345, cost=3)

        assert allowed
        # Verify incrby was called with cost=3
        mock_pipe.incrby.assert_called_once()
        call_args = mock_pipe.incrby.call_args
        assert call_args[0][1] == 3  # Second arg should be cost

    async def test_check_and_record_empty_result(self):
        """Test check_and_record handles empty Redis result."""
        mock_pipe = MagicMock()
        mock_pipe.incrby = MagicMock()
        mock_pipe.expire = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[])  # Empty result
        self.mock_redis.pipeline.return_value = mock_pipe

        allowed, _msg = await self.limiter.check_and_record(12345)

        # Should handle gracefully with count=0
        assert allowed

    async def test_ttl_calculation_with_cooldown_multiplier(self):
        """Test TTL calculation considers cooldown_multiplier."""
        config = RateLimitConfig(max_requests=5, window_seconds=10, cooldown_multiplier=3.0)
        limiter = RedisUserRateLimiter(self.mock_redis, config, "test")

        mock_pipe = MagicMock()
        mock_pipe.incrby = MagicMock()
        mock_pipe.expire = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[3, True])
        self.mock_redis.pipeline.return_value = mock_pipe

        await limiter.check_and_record(12345)

        # Verify expire was called with max(window+5, window*multiplier)
        # max(15, 30) = 30
        mock_pipe.expire.assert_called_once()
        call_args = mock_pipe.expire.call_args
        assert call_args[0][1] == 30  # TTL should be 30

    async def test_rate_limit_with_string_user_id(self):
        """Test rate limiter accepts string user IDs."""
        mock_pipe = MagicMock()
        mock_pipe.incrby = MagicMock()
        mock_pipe.expire = MagicMock()
        mock_pipe.execute = AsyncMock(return_value=[1, True])
        self.mock_redis.pipeline.return_value = mock_pipe

        allowed, _msg = await self.limiter.check_and_record("user_123", cost=1)

        assert allowed

    async def test_concurrent_with_string_user_id(self):
        """Test concurrent slot operations with string user IDs."""
        self.mock_redis.incr = AsyncMock(return_value=1)
        self.mock_redis.expire = AsyncMock()

        result = await self.limiter.acquire_concurrent_slot("user_456")

        assert result is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
