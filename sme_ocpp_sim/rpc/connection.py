"""Per-connection OCPP-J RPC: id correlation, timeouts, CALL serialization, CALLERROR.

Rules (design §5, "pitfalls"):
  * the pending-call table and the call lock belong to ONE connection, never to a class
  * at most one outstanding outbound CALL per connection (OCPP-J)
  * every inbound CALL runs in its own task, so a handler may itself send a CALL
    without dead-locking the receive loop
  * malformed frames and unknown actions are answered with a standard CALLERROR
  * a late (after timeout) or unsolicited response is logged and dropped; it never
    poisons the next call
  * :func:`after_this_reply` defers work until the answer to the inbound CALL being handled
    has been handed to the socket, so a follow-up (a close, a reconnect) can never overtake it
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from websockets.exceptions import ConnectionClosed

from . import framer
from .errors import OCPP16_ERRORS, CallError, ErrorCodes

log = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[dict]]


class _ReplyHooks:
    """Follow-ups registered while one inbound CALL is handled; ``done`` once its answer went out (or never will)."""

    def __init__(self) -> None:
        self.fns: list[Callable[[], None]] = []
        self.done = False


# The hooks of the inbound CALL handled in this context; None outside a handler. A task the handler spawns copies
# the context and so sees the same object: it may still register while the answer is pending, not after.
_reply_hooks: contextvars.ContextVar[Optional[_ReplyHooks]] = contextvars.ContextVar("reply_hooks", default=None)


def after_this_reply(fn: Callable[[], None]) -> bool:
    """Run ``fn`` right after the CALLRESULT/CALLERROR of the inbound CALL being handled was handed to the socket.

    Returns False (``fn`` not registered) outside a CALL handler or once its answer already went out; the caller
    then decides itself. ``fn`` is dropped when the handling task is cancelled (the connection went away).
    """
    hooks = _reply_hooks.get()
    if hooks is None or hooks.done:
        return False
    hooks.fns.append(fn)
    return True


@dataclass(frozen=True)
class FrameInfo:
    """One raw frame plus what the RPC layer knows about it (for the frame log).

    ``action`` is filled for responses too (from the matching CALL) when known.
    ``note`` explains drops and protocol problems, e.g. "late response dropped".
    """

    direction: str                 # "rx" | "tx"
    raw: str
    message_type: Optional[int] = None
    uid: Optional[str] = None
    action: Optional[str] = None
    note: str = ""


FrameCallback = Callable[[FrameInfo], None]


class RpcConnection:
    def __init__(
        self,
        ws: Any,
        handlers: Optional[dict[str, Handler]] = None,
        *,
        error_codes: ErrorCodes = OCPP16_ERRORS,
        call_timeout: float = 30.0,
        on_frame: Optional[FrameCallback] = None,
        name: str = "",
        uid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        call_guard: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._ws = ws
        self.handlers: dict[str, Handler] = dict(handlers or {})
        #: Called with the action of every inbound CALL before its handler is looked up; a :class:`CallError` it
        #: raises is the answer (e.g. a station that refuses every CALL in some state).
        self._call_guard = call_guard
        self.codes = error_codes
        self._timeout = call_timeout
        self._on_frame = on_frame
        self.name = name
        self._uid_factory = uid_factory
        self._pending: dict[str, tuple[asyncio.Future, str]] = {}
        self._call_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False
        self.closed = asyncio.Event()

    # ------------------------------------------------------------------ helpers
    def _emit(self, info: FrameInfo) -> None:
        if self._on_frame is None:
            return
        try:
            self._on_frame(info)
        except Exception:  # noqa: BLE001 - a broken observer must not break the RPC
            log.exception("%s: frame callback failed", self.name)

    async def _send_raw(self, raw: str, info: FrameInfo) -> None:
        self._emit(info)
        await self._ws.send(raw)

    async def _send_error(self, uid: str, code: str, desc: str,
                          details: Optional[dict] = None, action: Optional[str] = None) -> None:
        cap = self.codes.max_error_description_length
        if cap is not None and len(desc) > cap:
            desc = desc[:cap]
        raw = framer.encode_error(uid, code, desc, details)
        try:
            await self._send_raw(raw, FrameInfo("tx", raw, framer.CALLERROR, uid, action))
        except ConnectionClosed:
            pass

    # ------------------------------------------------------------------ tx
    async def call(self, action: str, payload: dict, timeout: Optional[float] = None) -> dict:
        """Send a CALL and return the CALLRESULT payload.

        Raises :class:`CallError` on CALLERROR, :class:`TimeoutError` on timeout
        and :class:`ConnectionError` if the connection is (or gets) closed.
        """
        async with self._call_lock:
            if self.closed.is_set() or self._stopping:
                raise ConnectionError("connection closed")
            uid = self._uid_factory()
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[uid] = (fut, action)
            try:
                raw = framer.encode_call(uid, action, payload)
                try:
                    await self._send_raw(raw, FrameInfo("tx", raw, framer.CALL, uid, action))
                except ConnectionClosed as e:
                    raise ConnectionError("connection closed") from e
                # asyncio.timeout, not wait_for: 3.11 wait_for may swallow a concurrent cancellation.
                async with asyncio.timeout(self._timeout if timeout is None else timeout):
                    return await fut
            finally:
                self._pending.pop(uid, None)

    # ------------------------------------------------------------------ rx
    async def run(self) -> None:
        """Receive loop. Returns when the connection is closed."""
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                await self._dispatch(raw)
        except ConnectionClosed:
            pass
        finally:
            self.closed.set()
            for fut, _ in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("connection closed"))
            for t in list(self._tasks):
                t.cancel()

    async def close(self) -> None:
        """Stop: no new calls, close the socket; :meth:`run` then returns."""
        self._stopping = True
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001 - closing an already broken socket
            pass

    async def _dispatch(self, raw: str) -> None:
        try:
            frame = framer.decode(raw)
        except framer.BadFrame as e:
            await self._on_bad_frame(raw, e)
            return

        if isinstance(frame, framer.Call):
            limit = self.codes.max_message_id_length
            if limit is not None and len(frame.uid) > limit and self.codes.rpc_framework_error is not None:
                # The answer echoes the long MessageId on purpose: it is readable and the only way to correlate.
                reason = f"MessageId longer than {limit} characters"
                self._emit(FrameInfo("rx", raw, framer.CALL, frame.uid, frame.action, reason))
                await self._send_error(frame.uid, self.codes.rpc_framework_error, reason, action=frame.action)
                return
            self._emit(FrameInfo("rx", raw, framer.CALL, frame.uid, frame.action))
            t = asyncio.create_task(self._handle_call(frame))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
            return

        mtype = framer.CALLRESULT if isinstance(frame, framer.CallResult) else framer.CALLERROR
        entry = self._pending.get(frame.uid)
        if entry is None or entry[0].done():
            note = "late or unsolicited response dropped"
            self._emit(FrameInfo("rx", raw, mtype, frame.uid, entry[1] if entry else None, note))
            log.warning("%s: response for unknown/expired id %s dropped", self.name, frame.uid)
            return
        fut, action = entry
        self._emit(FrameInfo("rx", raw, mtype, frame.uid, action))
        if isinstance(frame, framer.CallResult):
            fut.set_result(frame.payload)
        else:
            fut.set_exception(CallError(frame.code, frame.description, frame.details))

    async def _on_bad_frame(self, raw: str, e: framer.BadFrame) -> None:
        mtype = e.message_type if isinstance(e.message_type, int) else None
        if e.kind == framer.NO_UID:
            # The answer to a frame without a usable uniqueId is the version's choice
            # (ErrorCodes.bad_frame_reply); 1.6J defines none, so 1.6 logs and drops it.
            reply_uid = self.codes.bad_frame_reply
            if reply_uid is None:
                self._emit(FrameInfo("rx", raw, mtype, None, None, f"dropped: {e.reason}"))
                log.warning("%s: frame dropped (%s): %.200s", self.name, e.reason, raw)
                return
            self._emit(FrameInfo("rx", raw, mtype, None, None, e.reason))
            await self._send_error(reply_uid, self.codes.rpc_framework_error or self.codes.protocol_error, e.reason)
            return
        if e.kind == framer.BAD_RESULT:
            entry = self._pending.get(e.uid)
            action = entry[1] if entry else None
            self._emit(FrameInfo("rx", raw, mtype, e.uid, action, e.reason))
            if entry and not entry[0].done():
                entry[0].set_exception(CallError(self.codes.format_violation, e.reason))
            return
        self._emit(FrameInfo("rx", raw, mtype, e.uid, None, e.reason))
        if e.kind == framer.BAD_CALL:
            # A wrong CALL frame structure: the version's choice (2.0.1 RpcFrameworkError, Part 4 Table 8); 1.6J
            # defines none and keeps its format code (FormationViolation).
            code = self.codes.bad_call_frame or self.codes.format_violation
        elif e.kind == framer.BAD_PAYLOAD:
            # A payload that is not a JSON object: the version's format code (1.6J FormationViolation, 2.0.1
            # FormatViolation, Part 4 Table 8).
            code = self.codes.format_violation
        else:
            # UNKNOWN_TYPE: the version's MessageTypeNotSupported where it has one (2.0.1);
            # unspecified in 1.6J, where ProtocolError is our choice.
            code = self.codes.message_type_not_supported or self.codes.protocol_error
        await self._send_error(e.uid, code, e.reason)

    async def _handle_call(self, frame: framer.Call) -> None:
        hooks = _ReplyHooks()
        _reply_hooks.set(hooks)  # this task's own context: the handler below runs in it
        try:
            await self._answer_call(frame)
        finally:
            hooks.done = True
        for fn in hooks.fns:
            try:
                fn()
            except Exception:  # noqa: BLE001 - a broken follow-up must not break the RPC
                log.exception("%s: after-reply hook for %s failed", self.name, frame.action)

    async def _answer_call(self, frame: framer.Call) -> None:
        if self._call_guard is not None:
            try:
                self._call_guard(frame.action)
            except CallError as e:
                await self._send_error(frame.uid, e.code, e.description, e.details, frame.action)
                return
        handler = self.handlers.get(frame.action)
        if handler is None:
            await self._send_error(frame.uid, self.codes.not_implemented,
                                   f"action {frame.action} is not implemented", action=frame.action)
            return
        try:
            result = await handler(frame.payload)
        except CallError as e:
            await self._send_error(frame.uid, e.code, e.description, e.details, frame.action)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - every CALL must be answered
            log.exception("%s: handler %s failed", self.name, frame.action)
            await self._send_error(frame.uid, self.codes.internal_error, type(e).__name__,
                                   action=frame.action)
            return
        raw = framer.encode_result(frame.uid, result)
        try:
            await self._send_raw(raw, FrameInfo("tx", raw, framer.CALLRESULT, frame.uid, frame.action))
        except ConnectionClosed:
            pass
