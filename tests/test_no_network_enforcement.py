"""The `no_network` marker must actually block live network I/O.

Before this, the marker was purely declarative -- registered and applied to many
suites but enforced by nothing. These tests lock the enforcement installed by the
``enforce_no_network`` autouse fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import socket

import pytest

from tests.conftest import BlockedNetworkError


class TestNoNetworkGuardActive:
    pytestmark = pytest.mark.no_network

    def test_socket_connect_to_non_loopback_is_blocked(self) -> None:
        s = socket.socket()
        try:
            with pytest.raises(BlockedNetworkError):
                s.connect(("198.51.100.7", 80))  # TEST-NET-2: never routable
        finally:
            s.close()

    def test_create_connection_to_remote_host_is_blocked(self) -> None:
        # Blocked on the host string before any DNS resolution happens.
        with pytest.raises(BlockedNetworkError):
            socket.create_connection(("example.com", 80), timeout=0.01)

    def test_loopback_connect_is_not_blocked(self) -> None:
        # The guard must let loopback through: the real connect runs and fails
        # with an ordinary OSError (nothing listening), never BlockedNetworkError.
        s = socket.socket()
        s.settimeout(0.2)
        try:
            with pytest.raises(OSError) as exc_info:
                s.connect(("127.0.0.1", 65500))
            assert not isinstance(exc_info.value, BlockedNetworkError)
        finally:
            s.close()

    def test_guard_is_installed_for_marked_tests(self) -> None:
        assert socket.create_connection.__name__ == "guarded_create_connection"


def test_guard_is_absent_without_the_marker() -> None:
    # An unmarked test must see the real socket API: the fixture only patches for
    # marked tests and monkeypatch restores the originals on teardown.
    assert socket.create_connection.__name__ != "guarded_create_connection"
    assert "connect" not in vars(socket.socket) or (
        socket.socket.connect.__name__ != "guarded_connect"
    )
