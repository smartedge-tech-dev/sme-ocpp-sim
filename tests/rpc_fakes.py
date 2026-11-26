"""In-memory stand-in for a websockets connection, for RPC unit tests."""

from __future__ import annotations

import asyncio
import json

from websockets.exceptions import ConnectionClosedOK

_EOF = object()


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self.on_send = None  # optional callback(raw) to script the peer

    # peer side
    def feed(self, frame) -> None:
        self._inbox.put_nowait(frame if isinstance(frame, str) else json.dumps(frame))

    def sent_frames(self) -> list:
        return [json.loads(r) for r in self.sent]

    # connection side
    async def send(self, raw: str) -> None:
        if self._closed:
            raise ConnectionClosedOK(None, None)
        self.sent.append(raw)
        if self.on_send:
            self.on_send(raw)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._inbox.put_nowait(_EOF)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if item is _EOF:
            raise StopAsyncIteration
        return item
