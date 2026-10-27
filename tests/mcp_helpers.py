"""Drive the SDK-based MCP app in-process."""
from __future__ import annotations

import asyncio

import httpx

LEGACY = ("2025-03-26", "2025-06-18", "2025-11-25")
MODERN = "2026-07-28"
VERSIONS = (*LEGACY, MODERN)
PREFIX = "io.modelcontextprotocol/"


class McpTransport(httpx.ASGITransport):
    """An ASGI transport that runs the MCP app's lifespan for the client's lifetime.

    The lifespan runs in its own task, so a fixture may open the client in
    one task and close it in another.
    """

    def __init__(self, app):
        super().__init__(app=app)
        self._task = None
        self._stop = None

    async def __aenter__(self):
        started, self._stop = asyncio.Event(), asyncio.Event()

        async def hold():
            async with self.app.lifespan():
                started.set()
                await self._stop.wait()

        self._task = asyncio.create_task(hold())
        waiter = asyncio.create_task(started.wait())
        await asyncio.wait({self._task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if not started.is_set():
            waiter.cancel()
            await self._task  # Raises the lifespan's startup error.
        return self

    async def __aexit__(self, *exc):
        if self._task is not None:
            self._stop.set()
            await self._task
            self._task = None
