"""Unit tests for EventBus."""

from datetime import UTC, datetime

import pytest

from app.domain.events.summary_events import SummaryCreated, SummaryMarkedAsRead
from app.infrastructure.messaging.event_bus import EventBus


class TestEventBus:
    """Test suite for EventBus."""

    @pytest.fixture
    def event_bus(self):
        """Create a fresh event bus for each test."""
        return EventBus()

    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self, event_bus):
        """Test subscribing to and publishing events."""
        called = False
        received_event = None

        async def handler(event: SummaryCreated):
            nonlocal called, received_event
            called = True
            received_event = event

        event_bus.subscribe(SummaryCreated, handler)

        event = SummaryCreated(
            occurred_at=datetime.now(UTC),
            aggregate_id=1,
            summary_id=1,
            request_id=2,
            language="en",
            has_insights=False,
        )

        await event_bus.publish(event)

        assert called is True
        assert received_event == event

    @pytest.mark.asyncio
    async def test_multiple_handlers_for_same_event(self, event_bus):
        """Test multiple handlers subscribed to same event."""
        handler1_called = False
        handler2_called = False

        async def handler1(event: SummaryCreated):
            nonlocal handler1_called
            handler1_called = True

        async def handler2(event: SummaryCreated):
            nonlocal handler2_called
            handler2_called = True

        event_bus.subscribe(SummaryCreated, handler1)
        event_bus.subscribe(SummaryCreated, handler2)

        event = SummaryCreated(
            occurred_at=datetime.now(UTC),
            aggregate_id=1,
            summary_id=1,
            request_id=2,
            language="en",
            has_insights=False,
        )

        await event_bus.publish(event)

        assert handler1_called is True
        assert handler2_called is True

    @pytest.mark.asyncio
    async def test_publish_with_no_handlers(self, event_bus):
        """Test publishing event with no subscribed handlers."""
        event = SummaryCreated(
            occurred_at=datetime.now(UTC),
            aggregate_id=1,
            summary_id=1,
            request_id=2,
            language="en",
            has_insights=False,
        )

        # Should not raise error
        await event_bus.publish(event)

    @pytest.mark.asyncio
    async def test_handler_error_does_not_affect_other_handlers(self, event_bus):
        """Test that error in one handler doesn't stop other handlers."""
        handler2_called = False

        async def handler1(event: SummaryCreated):
            msg = "Handler 1 error"
            raise ValueError(msg)

        async def handler2(event: SummaryCreated):
            nonlocal handler2_called
            handler2_called = True

        event_bus.subscribe(SummaryCreated, handler1)
        event_bus.subscribe(SummaryCreated, handler2)

        event = SummaryCreated(
            occurred_at=datetime.now(UTC),
            aggregate_id=1,
            summary_id=1,
            request_id=2,
            language="en",
            has_insights=False,
        )

        await event_bus.publish(event)

        # Handler 2 should still be called even though handler 1 failed
        assert handler2_called is True

    def test_get_handler_count(self, event_bus):
        """Test getting handler count for an event type."""

        async def handler1(event: SummaryCreated):
            pass

        async def handler2(event: SummaryCreated):
            pass

        event_bus.subscribe(SummaryCreated, handler1)
        event_bus.subscribe(SummaryCreated, handler2)

        assert event_bus.get_handler_count(SummaryCreated) == 2
        assert event_bus.get_handler_count(SummaryMarkedAsRead) == 0

    def test_unsubscribe(self, event_bus):
        """Test unsubscribing a handler."""

        async def handler(event: SummaryCreated):
            pass

        event_bus.subscribe(SummaryCreated, handler)
        assert event_bus.get_handler_count(SummaryCreated) == 1

        event_bus.unsubscribe(SummaryCreated, handler)
        assert event_bus.get_handler_count(SummaryCreated) == 0

    def test_clear_handlers_for_specific_event(self, event_bus):
        """Test clearing handlers for specific event type."""

        async def handler1(event: SummaryCreated):
            pass

        async def handler2(event: SummaryMarkedAsRead):
            pass

        event_bus.subscribe(SummaryCreated, handler1)
        event_bus.subscribe(SummaryMarkedAsRead, handler2)

        event_bus.clear_handlers(SummaryCreated)

        assert event_bus.get_handler_count(SummaryCreated) == 0
        assert event_bus.get_handler_count(SummaryMarkedAsRead) == 1

    def test_clear_all_handlers(self, event_bus):
        """Test clearing all handlers."""

        async def handler1(event: SummaryCreated):
            pass

        async def handler2(event: SummaryMarkedAsRead):
            pass

        event_bus.subscribe(SummaryCreated, handler1)
        event_bus.subscribe(SummaryMarkedAsRead, handler2)

        event_bus.clear_handlers()

        assert event_bus.get_handler_count(SummaryCreated) == 0
        assert event_bus.get_handler_count(SummaryMarkedAsRead) == 0

    def test_get_all_event_types(self, event_bus):
        """Test getting all event types with subscribers."""

        async def handler1(event: SummaryCreated):
            pass

        async def handler2(event: SummaryMarkedAsRead):
            pass

        event_bus.subscribe(SummaryCreated, handler1)
        event_bus.subscribe(SummaryMarkedAsRead, handler2)

        event_types = event_bus.get_all_event_types()

        assert len(event_types) == 2
        assert SummaryCreated in event_types
        assert SummaryMarkedAsRead in event_types
