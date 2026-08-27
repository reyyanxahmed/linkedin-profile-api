"""Shared test configuration.

Enforces the HARD RULE that tests never touch the network. We register a conftest-level
guard that fails any test that actually opens a network connection, while still allowing
asyncio's internal socketpair (used for the event loop self-pipe) and AF_UNIX sockets
(which are local, not network).
"""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block real outbound network connections during tests.

    We override connect/connect_ex/connect_blocking to refuse any AF_INET/AF_INET6
    connection. AF_UNIX (local IPC, used by asyncio's self-pipe and TestClient) is
    allowed. This enforces rule 0.4 (tests never touch the network) without breaking
    asyncio's own machinery.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _blocked_connect(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Allow AF_UNIX (local IPC, not network).
        if self.family == socket.AF_UNIX:
            return real_connect(self, address, *args, **kwargs)
        # Block any INET/INET6 connection.
        raise AssertionError(
            "NETWORK BLOCKED: tests must not touch the network. "
            "Attempted connect to: " + repr(address)
        )

    def _blocked_connect_ex(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.family == socket.AF_UNIX:
            return real_connect_ex(self, address, *args, **kwargs)
        raise AssertionError(
            "NETWORK BLOCKED: tests must not touch the network. "
            "Attempted connect_ex to: " + repr(address)
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect_ex)
    # create_connection always makes an INET connection — block outright.
    def _refuse_create_connection(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(
            "NETWORK BLOCKED: tests must not touch the network. "
            "Attempted create_connection with: " + repr(args)
        )
    monkeypatch.setattr(socket, "create_connection", _refuse_create_connection)