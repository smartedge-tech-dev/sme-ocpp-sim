"""Minimal self-written OCPP-J RPC layer (spike).

Frames:
    CALL       [2, uniqueId, action, payload]
    CALLRESULT [3, uniqueId, payload]
    CALLERROR  [4, uniqueId, errorCode, errorDescription, errorDetails]

Design points (see ai/design §5 "pitfalls"):
  * pending-call table and send lock are held PER CONNECTION (never class-level)
  * at most one outstanding outbound CALL per connection (OCPP-J requirement)
  * malformed frames / unknown actions are answered with a standard CALLERROR
  * a late response (after timeout) or an unsolicited response is logged and dropped;
    it never poisons the next call (cf. MobilityHouse ocpp #790)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from websockets.exceptions import ConnectionClosed

log = logging.getLogger("rpc")

CALL, CALLRESULT, CALLERROR = 2, 3, 4


@dataclass(frozen=True)
class ErrorCodes:
    """Version-specific CALLERROR code names."""

    not_implemented: str = "NotImplemented"
    not_supported: str = "NotSupported"
    internal_error: str = "InternalError"
    protocol_error: str = "ProtocolError"
    security_error: str = "SecurityError"
    format_violation: str = "FormatViolation"
    property_constraint_violation: str = "PropertyConstraintViolation"
    occurrence_constraint_violation: str = "OccurrenceConstraintViolation"
    type_constraint_violation: str = "TypeConstraintViolation"
    generic_error: str = "GenericError"


# OCPP-J 1.6 §4.2.3 spells two codes wrongly; the errata sheet (§5.1/§5.2,
# "known issues that will not be fixed") keeps the misspellings on the wire.
OCPP16_ERRORS = ErrorCodes(
    format_violation="FormationViolation",
    occurrence_constraint_violation="OccurenceConstraintViolation",
)
# OCPP 2.0.1 uses FormatViolation but still OccurenceConstraintViolation;
# 2.0.1 also adds RpcFrameworkError etc. Not needed for this 1.6 spike.


class CallError(Exception):
    """Raised by call() when the peer answers with CALLERROR, or by a handler
    to make the RPC layer answer with a specific CALLERROR."""

    def __init__(self, code: str, description: str = "", details: Optional[dict] = None):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.details = details or {}


Handler = Callable[[dict], Awaitable[dict]]
FrameCallback = Callable[[str, str], None]  # (direction "rx"/"tx", raw text)


class RpcConnection:
    def __init__(
        self,
        ws: Any,
        handlers: dict[str, Handler],
        *,
        error_codes: ErrorCodes = OCPP16_ERRORS,
        call_timeout: float = 30.0,
        on_frame: Optional[FrameCallback] = None,
        name: str = "",
    ) -> None:
        self._ws = ws
        self._handlers = handlers
        self._codes = error_codes
        self._timeout = call_timeout
        self._on_frame = on_frame
        self._name = name
        # Per-connection state (pitfall #1 in design §5).
        self._pending: dict[str, asyncio.Future] = {}
        self._call_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.closed = asyncio.Event()

    # ------------------------------------------------------------------ tx
    async def _send(self, frame: list) -> None:
        raw = json.dumps(frame, separators=(",", ":"))
        if self._on_frame:
            self._on_frame("tx", raw)
        await self._ws.send(raw)

    async def call(self, action: str, payload: dict, timeout: Optional[float] = None) -> dict:
        """Send a CALL and wait for its CALLRESULT payload.

        Raises CallError on CALLERROR, asyncio.TimeoutError on timeout,
        ConnectionClosed if the connection drops while waiting.
        """
        async with self._call_lock:  # serialize: max 1 outstanding outbound CALL
            uid = str(uuid.uuid4())
            fut = asyncio.get_running_loop().create_future()
            self._pending[uid] = fut
            try:
                await self._send([CALL, uid, action, payload])
                return await asyncio.wait_for(fut, timeout or self._timeout)
            finally:
                self._pending.pop(uid, None)

    # ------------------------------------------------------------------ rx
    async def run(self) -> None:
        """Receive loop. Returns when the connection is closed."""
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                if self._on_frame:
                    self._on_frame("rx", raw)
                await self._dispatch(raw)
        except ConnectionClosed:
            pass
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("connection closed"))
            for t in list(self._tasks):
                t.cancel()

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            # TODO(spec): 1.6J does not say how to answer a frame whose uniqueId
            # cannot be extracted (2.0.1 answers with id "-1" + RpcFrameworkError).
            # For now: log and drop.
            log.warning("%s: non-JSON frame dropped: %.200s", self._name, raw)
            return
        if not isinstance(msg, list) or len(msg) < 3 or not isinstance(msg[1], str):
            # TODO(spec): same as above - no usable uniqueId.
            log.warning("%s: frame without usable uniqueId dropped: %.200s", self._name, raw)
            return

        mtype, uid = msg[0], msg[1]
        if mtype == CALL:
            if len(msg) != 4 or not isinstance(msg[2], str) or not isinstance(msg[3], dict):
                # TODO(spec): 1.6J has no dedicated "RPC framing" code; we use
                # FormationViolation for both bad frame shape and non-object payload.
                await self._send_error(uid, self._codes.format_violation,
                                       "CALL frame or payload is malformed")
                return
            t = asyncio.create_task(self._handle_call(uid, msg[2], msg[3]))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        elif mtype in (CALLRESULT, CALLERROR):
            fut = self._pending.get(uid)
            if fut is None or fut.done():
                log.warning("%s: response for unknown/expired id %s dropped", self._name, uid)
                return
            if mtype == CALLRESULT:
                if len(msg) != 3 or not isinstance(msg[2], dict):
                    fut.set_exception(CallError(self._codes.format_violation,
                                                "malformed CALLRESULT"))
                else:
                    fut.set_result(msg[2])
            else:
                code = msg[2] if len(msg) > 2 else self._codes.generic_error
                desc = msg[3] if len(msg) > 3 else ""
                details = msg[4] if len(msg) > 4 and isinstance(msg[4], dict) else {}
                fut.set_exception(CallError(str(code), str(desc), details))
        else:
            # TODO(spec): unknown MessageTypeId - 1.6J unspecified; we answer
            # ProtocolError since a uniqueId is available.
            await self._send_error(uid, self._codes.protocol_error,
                                   f"unknown MessageTypeId {mtype!r}")

    async def _handle_call(self, uid: str, action: str, payload: dict) -> None:
        handler = self._handlers.get(action)
        if handler is None:
            await self._send_error(uid, self._codes.not_implemented,
                                   f"action {action} not implemented")
            return
        try:
            result = await handler(payload)
        except CallError as e:
            await self._send_error(uid, e.code, e.description, e.details)
            return
        except Exception as e:  # noqa: BLE001 - must always answer
            log.exception("%s: handler %s failed", self._name, action)
            await self._send_error(uid, self._codes.internal_error, type(e).__name__)
            return
        try:
            await self._send([CALLRESULT, uid, result])
        except ConnectionClosed:
            pass

    async def _send_error(self, uid: str, code: str, desc: str, details: Optional[dict] = None) -> None:
        try:
            await self._send([CALLERROR, uid, code, desc, details or {}])
        except ConnectionClosed:
            pass
