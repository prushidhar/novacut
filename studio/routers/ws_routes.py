"""WebSocket route for real-time client sync."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import auth, config
from ..ws import manager

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    try:
        manager.set_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass

    cookie = websocket.cookies.get(auth.COOKIE, "")
    cached = auth._sess.get(auth._h(cookie)) if cookie else None
    user = (
        cached[1]
        if cached
        else (auth.user_for(cookie) if cookie and config.ACCOUNTS else (auth._local or auth.local_user()))
    )
    if not user:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, user.id)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except (WebSocketDisconnect, Exception):
        manager.disconnect(websocket, user.id)
