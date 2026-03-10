"""
WebSocket Manager
=================
Manages per-run WebSocket connections so the execution engine can broadcast
Playwright log lines to the React frontend in real time.

Pattern:
  - Frontend connects to  ws://localhost:8000/ws/run/{run_id}
  - Execution engine publishes lines to Redis channel  run:{run_id}:logs
  - A background subscriber task forwards Redis messages → WebSocket clients
"""
import asyncio
import logging
from collections import defaultdict
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    def __init__(self):
        # run_id → set of active WebSocket connections
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, run_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[run_id].add(ws)
        logger.info("WS connected  run_id=%s  total=%d", run_id, len(self._connections[run_id]))

    def disconnect(self, run_id: str, ws: WebSocket) -> None:
        self._connections[run_id].discard(ws)
        logger.info("WS disconnected run_id=%s", run_id)

    async def broadcast(self, run_id: str, message: str) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections.get(run_id, [])):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(run_id, ws)

    async def broadcast_json(self, run_id: str, data: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections.get(run_id, [])):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(run_id, ws)

    async def close_all(self, run_id: str, message: str = "") -> None:
        """Send a final message and close all sockets for a run."""
        if message:
            await self.broadcast(run_id, message)
        for ws in list(self._connections.get(run_id, [])):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.pop(run_id, None)


# ── Redis subscriber (background task) ──────────────────────────────────────────

async def redis_log_subscriber(run_id: str, manager: "WebSocketManager", redis_url: str) -> None:
    """
    Subscribe to Redis pub/sub channel for a run and forward messages to WebSocket.
    Run this as an asyncio.Task from the WebSocket endpoint.

    Strategy — ensures no messages are missed even if the client connects late:
      1. Subscribe to pub/sub FIRST so new messages queue up.
      2. Replay the history list (populated by github_actions_runner.pub()).
      3. If __DONE__ was already in history, return early (run already finished).
      4. Otherwise forward queued + new pub/sub messages until __DONE__.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(redis_url)
    channel = f"run:{run_id}:logs"
    history_key = f"run:{run_id}:log_history"
    pubsub = r.pubsub()

    # Step 1 — subscribe before reading history so we don't miss concurrent publishes
    await pubsub.subscribe(channel)

    # Step 2 — replay history for any messages published before we subscribed
    already_done = False
    try:
        history = await r.lrange(history_key, 0, -1)
        for item in history:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            await manager.broadcast(run_id, item)
            if item.strip() == "__DONE__":
                already_done = True
                break
    except Exception as e:
        logger.warning("Log history replay failed for run %s: %s", run_id, e)

    # Step 3 — if run already finished, no need to wait on pub/sub
    if already_done:
        await pubsub.unsubscribe(channel)
        await r.aclose()
        return

    # Step 4 — forward pub/sub messages (includes any buffered since we subscribed)
    try:
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                data = msg["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                await manager.broadcast(run_id, data)
                if data.strip() == "__DONE__":
                    break
    finally:
        await pubsub.unsubscribe(channel)
        await r.aclose()


# Singleton used across the app
ws_manager = WebSocketManager()
