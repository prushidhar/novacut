"""Real-time WebSocket notifications for client sync."""
from __future__ import annotations

import asyncio
import collections
import json
import threading
from typing import Any

from fastapi import WebSocket

from . import ctx


class ConnectionManager:
    """Manages active WebSocket connections per user and pushes sync notifications."""

    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = collections.defaultdict(set)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket, uid: str) -> None:
        await ws.accept()
        with self._lock:
            self._conns[uid].add(ws)

    def disconnect(self, ws: WebSocket, uid: str) -> None:
        with self._lock:
            self._conns[uid].discard(ws)
            if not self._conns[uid]:
                self._conns.pop(uid, None)

    def notify(self, uid: str) -> None:
        """Called whenever ctx.bump() is called to alert any connected tabs for this user."""
        with self._lock:
            sockets = list(self._conns.get(uid, ()))
        if not sockets or not self._loop or self._loop.is_closed():
            return

        payload = json.dumps({"type": "sync", "v": ctx.version(uid)})

        async def _broadcast() -> None:
            dead: list[WebSocket] = []
            for ws in sockets:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for d in dead:
                self.disconnect(d, uid)

        try:
            asyncio.run_coroutine_threadsafe(_broadcast(), self._loop)
        except RuntimeError:
            pass


manager = ConnectionManager()
ctx._notify_hook = manager.notify
