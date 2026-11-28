"""Network impairments on an OCPP-J websocket (v0.2.2 A, simulator-only).

:class:`ImpairedSocket` wraps a websockets connection (anything with ``send`` / async iteration / ``close``) and
delays the frames of one or both directions by ``latency_ms`` +- ``jitter_ms``.

- **Live settings**: a held frame waits until ``arrival + delay``, where the delay is recomputed from the current
  settings about every :data:`RECHECK_S` seconds; lowering or switching off the latency releases held frames.
- **Order**: frames are released one after the other in arrival order (sent frames under a FIFO lock, received
  frames by the single reader), so a short delay never overtakes a long one (reordering is out of scope).
  Received frames are read by a pump task as they arrive, so the delay of one frame does not add to the next one's.
- **Seeding**: each frame draws one jitter factor from a ``random.Random`` per direction, seeded from the
  configured seed (or a CRC32 of the charger id). The RNG is created per connection, so a seed change applies
  from the next connection and every connection starts the same sequence again.
- **Close / drop**: :meth:`ImpairedSocket.close` and :meth:`ImpairedSocket.drop` wake every held frame at once;
  received frames still queued are discarded, and a sent frame still held is lost (as on a real network), its
  ``send`` raising ``ConnectionClosed``. ``drop("abort")`` cuts the TCP connection without a close frame,
  ``drop("close")`` closes with code 1001 (falling back to an abort when the close handshake hangs).
- The websockets library's own ping / pong is not delayed. The receive queue is bounded, so a slow reader
  pushes back on the TCP connection instead of buffering without limit.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
import zlib
from typing import TYPE_CHECKING, Any, Callable, Optional

from websockets.exceptions import ConnectionClosedOK

if TYPE_CHECKING:  # the rpc layer does not import the config at runtime
    from ..config import ImpairmentConfig

log = logging.getLogger(__name__)

#: Close code of a ``close``-mode drop (1001 "going away").
DROP_CLOSE_CODE = 1001
#: How long a ``close``-mode drop waits for the close handshake before it aborts the connection.
DROP_CLOSE_TIMEOUT_S = 1.0
#: A held frame re-reads the settings at least this often (a lowered latency releases it).
RECHECK_S = 0.1
#: Received frames queued at most (then the pump stops reading: TCP back-pressure).
RX_QUEUE_MAX = 1000

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
    """A websocket whose frames are delayed per ``settings()`` (re-read while a frame is held)."""

    def __init__(self, ws: Any, settings: Callable[[], "ImpairmentConfig"], seed: int, *,
                 monotonic: Callable[[], float] = time.monotonic, rx_queue_max: int = RX_QUEUE_MAX) -> None:
        self._ws = ws
        self._settings = settings
        self._mono = monotonic
        self._rng = {"tx": make_rng(seed, "tx"), "rx": make_rng(seed, "rx")}
        self._tx_lock = asyncio.Lock()
        self._rx: asyncio.Queue = asyncio.Queue(maxsize=rx_queue_max)
        self._pump: Optional[asyncio.Task] = None
        self._closed = asyncio.Event()

    @property
    def inner(self) -> Any:
        return self._ws

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def __getattr__(self, name: str) -> Any:  # close_code, close_reason, transport, ...
        return getattr(self._ws, name)

    # ------------------------------------------------------------------ delays

    def _draw(self, direction: str) -> float:
        """The jitter factor (-1 .. 1) of the next frame of ``direction``; one draw per frame, always."""
        return self._rng[direction].uniform(-1.0, 1.0)

    def _delay(self, direction: str, factor: float) -> float:
        """The delay in seconds of a frame with jitter ``factor`` under the current settings."""
        s = self._settings()
        if not s.delays or s.direction not in ("both", direction):
            return 0.0
        return max(0.0, s.latency_ms + factor * s.jitter_ms) / 1000.0

    def next_delay(self, direction: str) -> float:
        """Draw the next frame's factor and return its delay now (tests / diagnostics)."""
        return self._delay(direction, self._draw(direction))

    async def _hold(self, direction: str, arrival: float, factor: float) -> bool:
        """Wait until ``arrival`` + the current delay; False when the socket closed meanwhile."""
        while not self._closed.is_set():
            wait = arrival + self._delay(direction, factor) - self._mono()
            if wait <= 0:
                return True
            try:
                async with asyncio.timeout(min(wait, RECHECK_S)):
                    await self._closed.wait()
            except TimeoutError:
                pass
        return False

    # ------------------------------------------------------------------ send

    async def send(self, raw: Any) -> None:
        arrival = self._mono()  # the delay counts from the call, not from getting the lock
        async with self._tx_lock:  # FIFO: frames are written in the order send() was called
            factor = self._draw("tx")
            if not await self._hold("tx", arrival, factor):
                raise ConnectionClosedOK(None, None)  # still held when the connection went: lost
            await self._ws.send(raw)

    # ------------------------------------------------------------------ receive

    def __aiter__(self) -> "ImpairedSocket":
        return self

    async def __anext__(self) -> Any:
        if self._pump is None and not self._closed.is_set():
            self._pump = asyncio.ensure_future(self._run_pump())
        item = await self._rx.get()
        if item is _EOF or self._closed.is_set():
            if self._rx.empty():
                self._rx.put_nowait(_EOF)  # later reads end too
            raise StopAsyncIteration
        if isinstance(item, _Error):
            raise item.exc
        arrival, factor, raw = item
        if not await self._hold("rx", arrival, factor):
            raise StopAsyncIteration  # closed while held: the frame is discarded
        return raw

    async def _run_pump(self) -> None:
        q = self._rx
        try:
            async for raw in self._ws:
                if self._closed.is_set():
                    return
                await q.put((self._mono(), self._draw("rx"), raw))  # bounded: back-pressure on the reader
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - handed to the reader, like the plain socket would raise it
            await q.put(_Error(e))
            return
        await q.put(_EOF)

    def _shut(self) -> None:
        """Mark closed: wake held frames, stop the pump, discard queued frames, end the reader."""
        self._closed.set()
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
        while not self._rx.empty():
            self._rx.get_nowait()
        self._rx.put_nowait(_EOF)

    # ------------------------------------------------------------------ close / drop

    async def close(self, *args: Any, **kwargs: Any) -> None:
        if not self._closed.is_set():
            self._shut()
        await self._ws.close(*args, **kwargs)

    async def drop(self, mode: str) -> None:
        """End the connection like a network failure: ``abort`` (no close frame) or ``close`` (code 1001)."""
        if not self._closed.is_set():
            self._shut()
        transport = getattr(self._ws, "transport", None)
        if mode == "abort" and transport is not None:
            transport.abort()
            return
        try:
            async with asyncio.timeout(DROP_CLOSE_TIMEOUT_S):
                if "code" in inspect.signature(self._ws.close).parameters:
                    await self._ws.close(code=DROP_CLOSE_CODE, reason="simulated drop")
                else:  # a socket whose close() takes no code (test fakes)
                    await self._ws.close()
        except TimeoutError:
            log.debug("close handshake of a simulated drop timed out; aborting")
            if transport is not None:
                transport.abort()

    async def aclose_pump(self) -> None:
        """Stop the receive pump (after the connection closed)."""
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
        if self._pump is not None:
            await asyncio.gather(self._pump, return_exceptions=True)
