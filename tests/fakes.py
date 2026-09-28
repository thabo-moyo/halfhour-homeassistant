"""Test doubles shared by the channel and setup tests."""

from __future__ import annotations

import json
from typing import Any


class FakeTransport:
    """Records what the channel asks of the broker; tests fire the callbacks.

    The channel sets on_connect / on_disconnect / on_message. Tests call them
    directly, as the real transport would once marshalled onto the HA loop.
    """

    def __init__(self, connect_error: Exception | None = None) -> None:
        self.connect_error = connect_error
        self.connects: list[dict[str, Any]] = []
        self.subscribed: list[list[tuple[str, int]]] = []
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.disconnects = 0
        self.on_connect: Any = lambda rc: None
        self.on_disconnect: Any = lambda rc: None
        self.on_message: Any = lambda topic, payload: None
        self.on_subscribe: Any = lambda codes: None

    def connect(self, host: str, port: int, tls: bool, ws: bool, username: str, password: str, will: tuple[str, bytes], keepalive: int, path: str = "/mqtt") -> None:
        self.connects.append(
            {"host": host, "port": port, "tls": tls, "ws": ws, "username": username, "password": password, "will": will, "keepalive": keepalive, "path": path}
        )
        if self.connect_error is not None:
            raise self.connect_error

    def subscribe(self, topics: list[tuple[str, int]]) -> None:
        self.subscribed.append(list(topics))

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.published.append((topic, payload, qos, retain))

    def disconnect(self) -> None:
        self.disconnects += 1

    # -- helpers for assertions ----------------------------------------------

    def sent(self, topic: str) -> list[dict[str, Any]]:
        """The JSON payloads published to one topic, in order."""
        return [json.loads(p) for t, p, _, _ in self.published if t == topic]
