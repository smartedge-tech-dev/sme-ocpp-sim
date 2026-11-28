"""Network impairments on an OCPP-J websocket (v0.2.2 A, simulator-only).

:class:`ImpairedSocket` wraps a websockets connection (anything with ``send`` / async iteration / ``close``) and
delays the frames of one or both directions by ``latency_ms`` +- ``jitter_ms``. The settings are read for every
frame, so a change applies to the open connection at once.

Order is preserved: each frame gets a due time ``max(arrival + delay, due time of the previous frame)``, so a
short delay drawn after a long one waits behind it (reordering is out of scope). Received frames are read by a
pump task as they arrive, so the delay of one frame does not add to the next one's. The delays come from one
``random.Random`` per direction, seeded from the configured seed (or the charger id): the same seed gives the
same delay sequence. The websockets library's own ping / pong is not delayed.

:meth:`ImpairedSocket.drop` ends the connection the way a network failure would: ``abort`` drops the TCP
connection without a close frame, ``close`` closes normally with code 1001.
"""

from __future__ import annotations

import asyncio
import random
import time
import zlib
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # the rpc layer does not import the config at runtime
    from ..config import ImpairmentConfig

#: Close code of a ``close``-mode drop (1001 "going away").
DROP_CLOSE_CODE = 1001

_EOF = object()


def impairment_seed(seed: Optional[int], charger_id: str) -> int:
    """The RNG seed: ``seed`` when given, else a CRC32 of the charger id (stable across runs)."""
    return seed if seed is not None else zlib.crc32(charger_id.encode("utf-8"))


def make_rng(seed: int, stream: str) -> random.Random:
    """A seeded RNG for one stream (``tx`` / ``rx`` / ``drop``): independent sequences from one seed."""
    return random.Random(f"{seed}:{stream}")


class _Error:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class ImpairedSocket:
    """A websocket whose frames are delayed per ``settings()`` (read for every frame)."""

    def __init__(self, ws: Any, settings: Callable[[], "ImpairmentConfig"], seed: int, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self._ws = ws
        self._settings = settings
        self._mono, self._sleep = monotonic, sleep
        self._rng = {"tx": make_rng(seed, "tx"), "rx": make_rng(seed, "rx")}
        self._due = {"tx": 0.0, "rx": 0.0}
        self._tx_lock = asyncio.Lock()
        self._rx: Optional[asyncio.Queue] = None
        self._pump: Optional[asyncio.Task] = None

    @property
    def inner(self) -> Any:
        return self._ws

    def __getattr__(self, name: str) -> Any:  # close_code, close_reason, transport, ...
        return getattr(self._ws, name)

    def next_delay(self, direction: str) -> float:
        """The delay in seconds of the next frame of ``direction`` (``tx`` / ``rx``); 0 when it is not delayed."""
        s = self._settings()
        if not s.delays or s.direction not in ("both", direction):
            return 0.0
        d = s.latency_ms + (self._rng[direction].uniform(-s.jitter_ms, s.jitter_ms) if s.jitter_ms else 0.0)
        return max(0.0, d) / 1000.0

    def _due_time(self, direction: str, arrival: float) -> float:
        due = max(arrival + self.next_delay(direction), self._due[direction])
        self._due[direction] = due
        return due

    async def _wait_until(self, due: float) -> None:
        wait = due - self._mono()
        if wait > 0:
            await self._sleep(wait)

    # ------------------------------------------------------------------ send

    async def send(self, raw: Any) -> None:
        due = self._due_time("tx", self._mono())  # fixed on entry: concurrent sends keep their order
        async with self._tx_lock:  # FIFO: frames are written in the order send() was called
            await self._wait_until(due)
            await self._ws.send(raw)

    # ------------------------------------------------------------------ receive

    def __aiter__(self) -> "ImpairedSocket":
        return self

    async def __anext__(self) -> Any:
        if self._rx is None:
            self._rx = asyncio.Queue()
            self._pump = asyncio.ensure_future(self._run_pump(self._rx))
        item = await self._rx.get()
        if item is _EOF:
            raise StopAsyncIteration
        if isinstance(item, _Error):
            raise item.exc
        due, raw = item
        await self._wait_until(due)
        return raw

    async def _run_pump(self, q: asyncio.Queue) -> None:
        try:
            async for raw in self._ws:
                q.put_nowait((self._due_time("rx", self._mono()), raw))
        except asyncio.CancelledError:
            q.put_nowait(_EOF)
            raise
        except Exception as e:  # noqa: BLE001 - handed to the reader, like the plain socket would raise it
            q.put_nowait(_Error(e))
            return
        q.put_nowait(_EOF)

    # ------------------------------------------------------------------ close / drop

    async def close(self, *args: Any, **kwargs: Any) -> None:
        await self._ws.close(*args, **kwargs)

    async def drop(self, mode: str) -> None:
        """End the connection like a network failure: ``abort`` (no close frame) or ``close`` (code 1001)."""
        if mode == "abort":
            transport = getattr(self._ws, "transport", None)
            if transport is not None:
                transport.abort()
                return
        try:
            await self._ws.close(code=DROP_CLOSE_CODE, reason="simulated drop")
        except TypeError:  # a socket whose close() takes no arguments (test fakes)
            await self._ws.close()

    async def aclose_pump(self) -> None:
        """Stop the receive pump (after the connection closed)."""
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
