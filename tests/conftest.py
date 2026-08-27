"""Shared test configuration.

Enforces the HARD RULE that tests never touch the network. We register a conftest-level
guard that fails any test that actually opens a socket, so a fixture or library cannot
silently introduce network dependency.
"""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all real socket connections during tests.

    If a test triggers a network call, it fails with a clear error rather than
    silently depending on a live endpoint. This is the test-side enforcement of
    BUILD_SPEC.md rule 0.4.
    """

    def _refuse(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(
            "NETWORK BLOCKED: tests must not touch the network. "
            "Use a fixture or mock instead. Args: " + repr(args)
        )

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)