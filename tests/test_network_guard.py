"""Sanity check: verify the network guard in conftest actually blocks sockets.

This test exists to prove the HARD RULE that tests never touch the network is enforced
mechanically, not just by convention. If this test passes, the guard works.
"""

from __future__ import annotations

import socket

import pytest


def test_socket_construction_blocked() -> None:
    with pytest.raises(AssertionError):
        socket.socket()


def test_create_connection_blocked() -> None:
    with pytest.raises(AssertionError):
        socket.create_connection(("example.com", 80))