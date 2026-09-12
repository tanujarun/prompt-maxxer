"""Localhost WebSocket between the engine and the shell's windows.

Events flow out to every window. Commands flow in from the settings window:
rebinding the hotkey, clearing the tape log, the mic monitor, downloading and
switching models. Nothing here can stall dictation: sends are fire and forget,
and commands are handed to the pipeline, which does any slow work on its own
threads.

Only the app's own windows may connect. Browsers do not apply the same-origin
policy to WebSockets, so without an Origin check any web page could open
ws://127.0.0.1:8765, read dictation as it happens, and send commands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http import HTTPStatus
from typing import Callable

log = logging.getLogger(__name__)

ALLOWED_ORIGINS = frozenset(
    {
        "http://tauri.localhost",  # Tauri 2 windows on Windows
        "https://tauri.localhost",
        "tauri://localhost",
        "http://localhost:1420",  # pnpm tauri dev
    }
)

CommandHandler = Callable[[str, dict], None]
SnapshotProvider = Callable[[], "list[dict]"]


class EventBus:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        on_command: CommandHandler | None = None,
        snapshot: SnapshotProvider | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._on_command = on_command
        # Called for every new connection. Windows come and go - settings is
        # created on demand and destroyed on close - so each must be able to
        # render the whole world the moment it connects.
        self._snapshot = snapshot
        self._clients: set = set()
        # Windows that asked for level frames while idle: the settings scope.
        self._monitors: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def monitoring(self) -> bool:
        return bool(self._monitors)

    # -- server thread ---------------------------------------------------

    @staticmethod
    def _check_origin(connection, request):
        origin = request.headers.get("Origin")
        # Browsers always send Origin on a WebSocket handshake; native clients
        # such as the test suite send none. A local process could read the
        # keyboard directly anyway, so it is browsers that need keeping out.
        if origin is None or origin in ALLOWED_ORIGINS:
            return None
        log.warning("refused a connection from origin %s", origin)
        return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")

    async def _handler(self, websocket) -> None:
        self._clients.add(websocket)
        origin = websocket.request.headers.get("Origin") if websocket.request else None
        log.info("window connected (%s, %d open)", origin or "no origin", len(self._clients))
        try:
            if self._snapshot is not None:
                for payload in self._snapshot():
                    await websocket.send(json.dumps(payload))
            async for raw in websocket:
                self._dispatch(websocket, raw)
        except Exception:
            log.debug("connection ended", exc_info=True)
        finally:
            self._clients.discard(websocket)
            self._monitors.discard(websocket)
            log.info("window disconnected (%d open)", len(self._clients))

    def _dispatch(self, websocket, raw) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict) or not isinstance(message.get("cmd"), str):
            return

        cmd = message["cmd"]
        if cmd == "monitor":
            if message.get("on"):
                self._monitors.add(websocket)
            else:
                self._monitors.discard(websocket)
            return

        if self._on_command is not None:
            try:
                self._on_command(cmd, message)
            except Exception:
                log.exception("command %r failed", cmd)

    async def _serve(self) -> None:
        import websockets

        async with websockets.serve(
            self._handler, self.host, self.port, process_request=self._check_origin
        ):
            log.info("event bus on ws://%s:%d", self.host, self.port)
            self._ready.set()
            await asyncio.Future()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            log.exception("event bus stopped")
            self._ready.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="prompt-maxxer-ipc", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    # -- publishing ------------------------------------------------------

    def emit(self, event: str, *, monitors_only: bool = False, **payload) -> None:
        """Broadcast an event. Never raises, never blocks the caller."""
        targets = self._monitors if monitors_only else self._clients
        if self._loop is None or not targets:
            return
        message = json.dumps({"event": event, **payload})
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(message, monitors_only), self._loop)
        except Exception:
            log.debug("emit failed", exc_info=True)

    async def _broadcast(self, message: str, monitors_only: bool) -> None:
        targets = self._monitors if monitors_only else self._clients
        for ws in list(targets):
            try:
                await ws.send(message)
            except Exception:
                self._clients.discard(ws)
                self._monitors.discard(ws)
