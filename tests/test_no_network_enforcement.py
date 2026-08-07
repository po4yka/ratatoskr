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

    def test_getaddrinfo_for_non_loopback_host_is_blocked(self) -> None:
        # A lookup that never reaches a connect is still egress, and on macOS the
        # resolve is where the damage starts: a successful non-loopback lookup with
        # a port arms Network.framework's at-fork child handler, which then
        # segfaults every later fork() in the process.
        with pytest.raises(BlockedNetworkError):
            socket.getaddrinfo("example.com", 443)

    def test_getaddrinfo_is_blocked_even_without_a_port(self) -> None:
        # port=None does not arm the fork bomb, but it is still a live query and a
        # marked test must not make one. Pins the guard as deliberately broader
        # than the crash it was prompted by.
        with pytest.raises(BlockedNetworkError):
            socket.getaddrinfo("example.com", None)

    def test_loopback_getaddrinfo_is_not_blocked(self) -> None:
        # Loopback resolution never leaves the host, so local collaborators keep
        # working unchanged.
        assert socket.getaddrinfo("127.0.0.1", 6333)
        assert socket.getaddrinfo("localhost", 6333)

    def test_guard_is_installed_for_marked_tests(self) -> None:
        assert socket.create_connection.__name__ == "guarded_create_connection"
        assert socket.getaddrinfo.__name__ == "guarded_getaddrinfo"


def test_guard_is_absent_without_the_marker() -> None:
    # An unmarked test must see the real socket API: the fixture only patches for
    # marked tests and monkeypatch restores the originals on teardown.
    assert socket.create_connection.__name__ != "guarded_create_connection"
    assert socket.getaddrinfo.__name__ != "guarded_getaddrinfo"
    assert "connect" not in vars(socket.socket) or (
        socket.socket.connect.__name__ != "guarded_connect"
    )
