"""Sanity check: verify the network guard in conftest actually blocks connections.

This test exists to prove the HARD RULE that tests never touch the network is enforced
mechanically, not just by convention. If this test passes, the guard works.
"""

from __future__ import annotations

import socket

import pytest


def test_inet_connect_blocked() -> None:
    """An AF_INET connect must raise, proving the network guard is active."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(AssertionError):
        s.connect(("example.com", 80))


def test_create_connection_blocked() -> None:
    with pytest.raises(AssertionError):
        socket.create_connection(("example.com", 80))


def test_unix_socket_allowed() -> None:
    """AF_UNIX (local IPC, used by asyncio's self-pipe) must NOT be blocked.

    This is what lets the test suite run an in-process ASGI app without tripping
    the network guard.
    """
    # We don't actually need to connect; just confirm constructing and binding an
    # AF_UNIX socket does not raise.
    a, b = socket.socketpair(family=socket.AF_UNIX)
    a.close()
    b.close()