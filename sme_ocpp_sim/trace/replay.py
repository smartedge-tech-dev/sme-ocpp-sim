"""Trace replay against a CSMS (v0.3 plan §3.7, Q8 = A): ``ocppsim-replay/1``.

Charger-side, raw-frame replay: the replay plays ONE charger of a trace and re-sends the CALLs that charger sent
(``direction=tx``) to a CSMS over a new WebSocket connection, then compares the CSMS answers of this run with the
answers in the trace. It does not run the station state machine; what is sent is the recorded frames, rewritten
only where a replay has to (:mod:`.rewrite`, :mod:`.rewrite_v16`, :mod:`.rewrite_v201`):

* a new ``uniqueId`` per CALL (the RPC layer draws one);
* every ISO 8601 date-time (any depth, DataTransfer data included) shifted by one offset per frame, computed when
  the frame is sent: the earliest timestamp of that frame becomes "now", offsets inside the frame are kept;
* 1.6: the ``transactionId`` the CSMS answers to StartTransaction now replaces the recorded one in later frames;
* 2.0.1 / 2.1: the charger-generated ``transactionId`` gets a run suffix (``tx_suffix``, default on);
* ``as_id`` (CLI ``--as``) connects with another charger identity (URL path and Basic Auth user).

Sending follows OCPP-J: one CALL at a time, the next one only after the answer (or the call timeout). Timing
(``timing``): ``original`` keeps the recorded gap between two CALLs (measured from the previous send; a slow answer
pushes the next CALL back), ``scale:N`` divides the gaps by N, ``asap`` sends the next CALL right after the answer;
``max_gap`` caps each wait. ``session`` replays only the n-th connection of the charger.
2.1 SEND frames (type 6) are re-sent in order, without an answer.

CALLs the CSMS sends during the replay are answered with the answer the charger gave in the trace, matched by
action and occurrence (the n-th TriggerMessage gets the n-th recorded answer; past the last one the last is
re-used). A CALLERROR in the trace is answered as that CALLERROR, an unanswered one is not answered. An action the
trace never answered is ``unknown_call``: ``reject`` (default) answers CALLERROR ``NotImplemented``, ``noreply``
does not answer.

The trace is masked (Q7), so credentials come as arguments: ``password`` (Security Profile 1 / 2 Basic Auth),
``ca_file`` / ``verify_tls`` and ``client_cert_file`` / ``client_key_file`` (profile 3).

Report ``ocppsim-replay/1``: per CALL the recorded and the replayed outcome (``result`` with its status fields,
``error`` with its code, ``timeout``, ``closed``, ``not_sent``; ``none`` = no answer in the trace, not compared)
and the differences; a difference is not a failure by itself (``exit_code(fail_on_diff=True)`` makes it one).
``result`` = ``match`` / ``diff`` / ``error`` (connection failed or lost, stopped). The run is recorded into a new
trace when ``out_trace`` is given.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import secrets
import time
from urllib.parse import urlsplit
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .. import __version__
from ..config import _charger_id
from ..framelog import SECRET_MASK, FrameLog
from ..rpc.connection import FrameInfo, RpcConnection
from ..rpc.errors import OCPP16_ERRORS, OCPP21_ERRORS, OCPP201_ERRORS, CallError
from ..rpc.transport import HandshakeError, TransportConfig, open_connection, wait_event
from . import format as fmt
from .recorder import TraceRecorder
from .rewrite import Rewriter
from .rewrite_v16 import Rewriter16
from .rewrite_v201 import Rewriter21, Rewriter201

FORMAT = "ocppsim-replay/1"
VERSIONS = ("1.6", "2.0.1", "2.1")
SUBPROTOCOLS = {"1.6": "ocpp1.6", "2.0.1": "ocpp2.0.1", "2.1": "ocpp2.1"}
ERROR_CODES = {"1.6": OCPP16_ERRORS, "2.0.1": OCPP201_ERRORS, "2.1": OCPP21_ERRORS}
UNKNOWN_CALL_MODES = ("reject", "noreply")
#: Actions only a 2.0.1 / 2.1 station sends (version detection of a v0 trace without ``ocpp_version``).
_V201_ACTIONS = frozenset({"TransactionEvent", "NotifyReport", "NotifyEvent", "ReportChargingProfiles",
                           "NotifyChargingLimit", "ClearedChargingLimit", "NotifyEVChargingNeeds"})
#: Top-level payload keys that only 2.0.1 / 2.1 messages have.
_V201_KEYS = frozenset({"transactionInfo", "evse", "eventType", "chargingStation"})
#: Longest inbound (CSMS CALL) list kept in the report; the rest is only counted.
MAX_INBOUND = 1000


class ReplayError(ValueError):
    """The replay cannot start (bad option, nothing to replay)."""


def parse_timing(text: Optional[str]) -> Optional[float]:
    """``original`` -> 1.0, ``scale:N`` -> N, ``asap`` -> ``None`` (no waiting)."""
    t = (text or "original").strip().lower()
    if t == "original":
        return 1.0
    if t == "asap":
        return None
    if t.startswith("scale:"):
        try:
            n = float(t[6:])
        except ValueError:
            n = 0.0
        if n > 0 and math.isfinite(n):
            return n
    raise ReplayError(f"timing {text!r}: original, scale:N (N > 0) or asap")


def _parse(raw: Any) -> Optional[list]:
    try:
        f = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return f if isinstance(f, list) and len(f) >= 3 and f[0] in (2, 3, 4, 5, 6) and isinstance(f[1], str) else None


@dataclass
class RecordedCall:
    index: int
    seq: Any
    ts: float
    action: str
    payload: Dict[str, Any]
    kind: str = "call"                 # "call" | "send" (2.1 SEND: no answer)
    response: Optional[list] = None    # the recorded [3, ...] / [4, ...] frame; None = no answer in the trace


@dataclass
class Plan:
    charger: str
    ocpp_version: str
    calls: List[RecordedCall]
    #: CSMS CALL action -> the recorded answer frames in order (None = the charger did not answer).
    answers: Dict[str, List[Optional[list]]] = field(default_factory=dict)
    skipped: int = 0                   # tx frames that could not be parsed
    notes: List[str] = field(default_factory=list)   # what the reader of the report should know about the trace


def build_plan(trace: fmt.Trace, charger: Optional[str] = None, ocpp_version: Optional[str] = None,
               session: Optional[int] = None) -> Plan:
    """What a replay of ``charger`` (the only charger of the trace when ``None``) sends and answers."""
    frames = [f for f in trace.frames if isinstance(f, dict) and isinstance(f.get("ts"), (int, float))]
    ids = sorted({str(f["charger"]) for f in frames if f.get("charger") is not None})
    if charger is None:
        if len(ids) != 1:
            raise ReplayError("the trace has no frames" if not ids else
                              f"the trace has chargers {', '.join(ids)}: choose one (--charger)")
        charger = ids[0]
    elif charger not in ids:
        raise ReplayError(f"the trace has no frames of charger {charger!r}")
    mine = sorted((f for f in frames if str(f.get("charger")) == charger),
                  key=lambda f: (f["ts"], f.get("seq") if isinstance(f.get("seq"), int) else 0))
    notes: List[str] = []
    starts = sorted(float(e["ts"]) for e in trace.events if isinstance(e, dict) and str(e.get("charger")) == charger
                    and e.get("event") == "connected" and isinstance(e.get("ts"), (int, float)))
    if session is not None:
        if not starts:
            raise ReplayError("the trace has no connection events: --session needs a v1 trace")
        if session > len(starts):
            raise ReplayError(f"session {session}: the trace has {len(starts)} session(s) of charger {charger!r}")
        lo = starts[session - 1]
        hi = starts[session] if session < len(starts) else float("inf")
        mine = [f for f in mine if lo <= f["ts"] < hi]
        notes.append(f"only session {session} of {len(starts)} was replayed")
    elif len(starts) > 1:
        notes.append(f"the trace has {len(starts)} sessions of charger {charger!r}; they are replayed merged on one "
                     "connection (--session N replays one)")
    if trace.incomplete:
        notes.append("the trace is incomplete (a part is missing or its footer has an error): the replay may lack CALLs")
    if trace.dropped > 0:
        notes.append(f"the recording dropped {trace.dropped} line(s) (write queue full): the replay may lack CALLs")
    calls: List[RecordedCall] = []
    answers: Dict[str, List[Optional[list]]] = defaultdict(list)
    open_tx: Dict[str, RecordedCall] = {}
    open_rx: Dict[str, tuple] = {}
    skipped = 0
    for f in mine:
        d, fr = f.get("direction"), _parse(f.get("raw"))
        if fr is None:
            skipped += d == "tx"
            continue
        mt, uid = fr[0], fr[1]
        if mt in (2, 6) and len(fr) >= 4 and isinstance(fr[2], str) and isinstance(fr[3], dict):
            if d == "tx":
                rc = RecordedCall(len(calls), f.get("seq"), float(f["ts"]), fr[2], fr[3],
                                  "send" if mt == 6 else "call")
                calls.append(rc)
                if mt == 2:
                    open_tx[uid] = rc
            elif d == "rx" and mt == 2:
                answers[fr[2]].append(None)
                open_rx[uid] = (fr[2], len(answers[fr[2]]) - 1)
        elif mt in (3, 4, 5):
            if d == "rx" and uid in open_tx:
                open_tx.pop(uid).response = fr
            elif d == "tx" and uid in open_rx:
                action, i = open_rx.pop(uid)
                answers[action][i] = fr
        elif d == "tx":
            skipped += 1
    if not calls:
        raise ReplayError(f"the trace has no CALL that charger {charger!r} sent")
    version = ocpp_version or trace.ocpp_version(charger)
    if version is None:
        version = "2.0.1" if any(c.action in _V201_ACTIONS or _V201_KEYS & set(c.payload) for c in calls) else "1.6"
        notes.append(f"the OCPP version was guessed ({version}) from the payloads: pass --ocpp-version for a trace "
                     "without a header")
    if version not in VERSIONS:
        raise ReplayError(f"OCPP version {version!r}: one of {', '.join(VERSIONS)}")
    if version == "1.6":
        _notes16(calls, notes)
    masked = sum(_count_masked(c.payload) for c in calls) + sum(
        _count_masked(fr[2]) for lst in answers.values() for fr in lst if fr and fr[0] == 3 and len(fr) > 2)
    if masked:
        notes.append(f"{masked} value(s) are masked ({SECRET_MASK!r}) in the trace and are sent as they are (a "
                     "secret the CSMS checks will not match)")
    return Plan(charger, version, calls, dict(answers), skipped, notes)


def _count_masked(obj: Any) -> int:
    if isinstance(obj, str):
        return int(obj == SECRET_MASK)
    if isinstance(obj, dict):
        return sum(_count_masked(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_masked(v) for v in obj)
    return 0


def _notes16(calls: List[RecordedCall], notes: List[str]) -> None:
    """1.6 transactionId edge cases of the trace itself (the CSMS assigns the id in StartTransaction.conf)."""
    started = set()
    for c in calls:
        if c.action == "StartTransaction" and c.kind == "call":
            ok = c.response is not None and c.response[0] == 3 and isinstance(c.response[2], dict)
            tid = c.response[2].get("transactionId") if ok else None
            if isinstance(tid, int):
                started.add(tid)
            else:
                notes.append(f"CALL #{c.index} StartTransaction has no answer in the trace: its transactionId is unknown")
    outside = sorted({c.payload["transactionId"] for c in calls if c.action in ("StopTransaction", "MeterValues")
                      and isinstance(c.payload.get("transactionId"), int) and c.payload["transactionId"] not in started})
    for tid in outside:
        notes.append(f"transactionId {tid} was started outside the trace: Stop / MeterValues send it unchanged")


def statuses(payload: Any) -> Dict[str, Any]:
    """The status-like fields of a payload (keys ``status`` / ``...Status``) by dotted path."""
    out: Dict[str, Any] = {}

    def rec(obj: Any, prefix: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{prefix}.{k}" if prefix else str(k)
                if isinstance(v, (dict, list)):
                    rec(v, p)
                elif k == "status" or str(k).endswith("Status"):
                    out[p] = v
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                rec(v, f"{prefix}[{i}]")

    rec(payload, "")
    return out


def recorded_outcome(fr: Optional[list]) -> Dict[str, Any]:
    if fr is None:
        return {"kind": "none"}
    if fr[0] == 3:
        return {"kind": "result", "status": statuses(fr[2] if len(fr) > 2 else {})}
    return {"kind": "error", "code": fr[2] if len(fr) > 2 else None}


def compare(recorded: Dict[str, Any], replayed: Dict[str, Any]) -> tuple:
    """``(match, differences)``; ``match`` is ``None`` when the trace has nothing to compare with."""
    rk, pk = recorded["kind"], replayed["kind"]
    if rk == "none" or pk == "sent":
        return None, []
    if rk != pk:
        a = f"{rk} {recorded.get('code')}" if rk == "error" else rk
        b = f"{pk} {replayed.get('code')}" if pk == "error" else pk
        return False, [f"{a} -> {b}"]
    if rk == "error":
        ok = recorded.get("code") == replayed.get("code")
        return ok, [] if ok else [f"error code: {recorded.get('code')} -> {replayed.get('code')}"]
    a, b = recorded.get("status", {}), replayed.get("status", {})
    diff = [f"{k}: {a.get(k)} -> {b.get(k)}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    return not diff, diff


def make_rewriter(version: str, *, shift=None, run_id: str, tx_suffix: bool = True, auto_shift: bool = True) -> Rewriter:
    if version == "1.6":
        return Rewriter16(shift=shift, run_id=run_id, auto_shift=auto_shift)
    cls = Rewriter21 if version == "2.1" else Rewriter201
    return cls(shift=shift, run_id=run_id, tx_suffix=tx_suffix, auto_shift=auto_shift)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


class _AnyAction(dict):
    """Handler table that hands every inbound CALL to the replayer."""

    def __init__(self, replayer: "Replayer") -> None:
        super().__init__()
        self._r = replayer

    def get(self, action, default=None):  # noqa: D401 - dict API
        return lambda payload: self._r._answer(action, payload)


class Replayer:
    """One replay run; :meth:`run` returns the ``ocppsim-replay/1`` report (also kept in :attr:`report`)."""

    def __init__(self, trace: fmt.Trace, *, csms_url: str, charger: Optional[str] = None,
                 as_id: Optional[str] = None, timing: str = "original", session: Optional[int] = None,
                 max_gap: Optional[float] = None, password: Optional[str] = None,
                 ocpp_version: Optional[str] = None, unknown_call: str = "reject", tx_suffix: bool = True,
                 call_timeout: float = 30.0, ca_file: Optional[str] = None, verify_tls: bool = True,
                 client_cert_file: Optional[str] = None, client_key_file: Optional[str] = None,
                 out_trace: Optional[Union[str, Path]] = None, trace_name: Optional[str] = None,
                 run_id: Optional[str] = None) -> None:
        if not csms_url:
            raise ReplayError("csms_url is required")
        try:
            parts = urlsplit(csms_url)
            ok = parts.scheme.lower() in ("ws", "wss") and bool(parts.hostname)
        except ValueError:
            ok = False
        if not ok:
            raise ReplayError("csms_url must be a ws:// or wss:// URL")
        if as_id is not None:
            try:
                _charger_id(as_id)
                if not as_id:
                    raise ValueError("empty")
            except ValueError as e:
                raise ReplayError(f"as_id {as_id!r}: {e}") from None
        if max_gap is not None and not (max_gap > 0 and math.isfinite(max_gap)):
            raise ReplayError("max_gap must be > 0")
        if unknown_call not in UNKNOWN_CALL_MODES:
            raise ReplayError(f"unknown_call {unknown_call!r}: reject or noreply")
        if not call_timeout or call_timeout <= 0:
            raise ReplayError("call_timeout must be > 0")
        self.factor = parse_timing(timing)
        self.timing = (timing or "original").strip().lower()
        self.trace = trace
        self.plan = build_plan(trace, charger, ocpp_version, session)
        self.session = session
        self.max_gap = max_gap
        self.identity = as_id or self.plan.charger
        self.unknown_call = unknown_call
        self.tx_suffix = tx_suffix
        self.call_timeout = call_timeout
        self.run_id = run_id or secrets.token_hex(3)
        self.out_trace = Path(out_trace) if out_trace else None
        self.trace_name = trace_name or (trace.parts[0] if trace.parts else None)
        self._transport = TransportConfig(
            csms_url=csms_url, charger_id=self.identity, subprotocol=SUBPROTOCOLS[self.plan.ocpp_version],
            password=password, ca_file=ca_file, verify_tls=verify_tls, client_cert_file=client_cert_file,
            client_key_file=client_key_file)
        self.state = "pending"          # pending | running | done
        self.sent = 0
        self.error: Optional[str] = None
        self.report: Optional[Dict[str, Any]] = None
        self.entries: List[Dict[str, Any]] = []
        self.inbound: List[Dict[str, Any]] = []        # at most MAX_INBOUND entries
        self._inbound_total = 0
        self._inbound_unknown = 0
        self.rewriter: Optional[Rewriter] = None
        self._seen: Dict[str, int] = defaultdict(int)
        self._stop: Optional[asyncio.Event] = None
        self._conn: Optional[RpcConnection] = None
        self._started = 0.0

    # ------------------------------------------------------------ control

    def status(self) -> Dict[str, Any]:
        return {"id": self.run_id, "state": self.state, "charger": self.plan.charger, "as": self.identity,
                "ocpp_version": self.plan.ocpp_version, "timing": self.timing, "max_gap": self.max_gap,
                "session": self.session, "sent": self.sent,
                "total": len(self.plan.calls), "error": self.error, "report": self.report}

    def stop(self) -> None:
        """End the run after the CALL in flight (the rest is reported as ``not_sent``)."""
        if self.error is None and self.state == "running":
            self.error = "stopped"
        if self._stop is not None:
            self._stop.set()     # also ends the wait for the answer of the CALL in flight

    # ------------------------------------------------------------ inbound CALLs

    async def _answer(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        n = self._seen[action]
        self._seen[action] += 1
        recorded = self.plan.answers.get(action) or []
        entry: Dict[str, Any] = {"action": action, "occurrence": n + 1}
        self._inbound_total += 1
        if len(self.inbound) < MAX_INBOUND:
            self.inbound.append(entry)
        if not recorded:
            self._inbound_unknown += 1
            entry["answer"] = "no_reply" if self.unknown_call == "noreply" else "not_implemented"
            entry["in_trace"] = False
            if self.unknown_call == "noreply":
                await asyncio.Event().wait()     # never answered; cancelled when the connection closes
            raise CallError(ERROR_CODES[self.plan.ocpp_version].not_implemented,
                            f"{action} is not in the replayed trace")
        entry["in_trace"] = True
        entry["reused"] = n >= len(recorded)
        fr = recorded[min(n, len(recorded) - 1)]
        if fr is None:
            entry["answer"] = "no_reply"
            await asyncio.Event().wait()
        if fr[0] == 3:
            entry["answer"] = "result"
            return self.rewriter.reply(action, fr[2] if isinstance(fr[2], dict) else {})
        entry["answer"] = f"error {fr[2]}"
        raise CallError(str(fr[2]), str(fr[3]) if len(fr) > 3 and fr[3] is not None else "",
                        fr[4] if len(fr) > 4 and isinstance(fr[4], dict) else None)

    # ------------------------------------------------------------ run

    async def run(self) -> Dict[str, Any]:
        self.state = "running"
        self._stop = asyncio.Event()
        self._started = time.time()
        self.rewriter = make_rewriter(self.plan.ocpp_version, run_id=self.run_id, tx_suffix=self.tx_suffix)
        recorder = self._recorder()
        try:
            await self._run(recorder)
        except Exception as e:  # noqa: BLE001 - the report must still be written
            self.error = self.error or f"{type(e).__name__}: {e}"
        finally:
            if recorder is not None:
                recorder.stop()
            self.state = "done"
            self.report = self._report()
        return self.report

    def _recorder(self) -> Optional[TraceRecorder]:
        if self.out_trace is None:
            return None
        src = self.trace.chargers.get(self.plan.charger, {})
        info = {"id": self.identity, "ocpp_version": self.plan.ocpp_version, "connectors": src.get("connectors"),
                "csms_url": fmt.strip_credentials(self._transport.csms_url),
                "subprotocol": self._transport.subprotocol,
                "replay_of": {"trace": self.trace_name, "charger": self.plan.charger, "run_id": self.run_id}}
        return TraceRecorder(self.out_trace, tool_version=__version__, chargers_info=[info]).start()

    async def _run(self, recorder: Optional[TraceRecorder]) -> None:
        framelog = FrameLog(maxlen=1)

        def on_frame(info: FrameInfo) -> None:
            entry = framelog.add(self.identity, info)
            if recorder is not None:
                recorder.frame(entry)

        try:
            ws = await open_connection(self._transport)
        except HandshakeError as e:
            self.error = f"connect failed: {e}"
            if recorder is not None:
                recorder.event(self.identity, "connect_failed", {"error": str(e)})
            return
        if recorder is not None:
            recorder.event(self.identity, "connected", {"replay": self.run_id})
        conn = self._conn = RpcConnection(ws, error_codes=ERROR_CODES[self.plan.ocpp_version],
                                          call_timeout=self.call_timeout, on_frame=on_frame, name=self.identity)
        conn.handlers = _AnyAction(self)
        reader = asyncio.create_task(conn.run())
        try:
            await self._send_all(conn)
        finally:
            await conn.close()
            try:
                async with asyncio.timeout(5):
                    await reader
            except (TimeoutError, asyncio.CancelledError):
                reader.cancel()
            if recorder is not None:
                recorder.event(self.identity, "disconnected", {"reason": self.error or "replay done"})

    async def _send_all(self, conn: RpcConnection) -> None:
        prev_rec: Optional[float] = None
        prev_sent = 0.0
        for rc in self.plan.calls:
            if self._stop.is_set() or conn.closed.is_set():
                if self.error is None:
                    self.error = "connection closed by the CSMS"
                break
            if self.factor is not None and prev_rec is not None:
                gap = max(0.0, rc.ts - prev_rec) / self.factor
                if self.max_gap is not None:
                    gap = min(gap, self.max_gap)
                wait = prev_sent + gap - time.monotonic()
                if wait > 0 and await wait_event(self._stop, wait):
                    break
            prev_rec, prev_sent = rc.ts, time.monotonic()
            payload = self.rewriter.call(rc.action, rc.payload)
            new: Optional[Dict[str, Any]] = None
            try:
                if rc.kind == "send":
                    await conn.send(rc.action, payload)
                    replayed: Dict[str, Any] = {"kind": "sent"}
                else:
                    new = await self._until_stopped(conn.call(rc.action, payload))
                    replayed = {"kind": "result", "status": statuses(new)}
            except CallError as e:
                replayed = {"kind": "error", "code": e.code}
            except TimeoutError:
                replayed = {"kind": "timeout"}
            except ConnectionError:
                replayed = {"kind": "closed"}
                self.error = self.error or "connection closed by the CSMS"
            replayed["latency_ms"] = round((time.monotonic() - prev_sent) * 1000.0, 1)
            if rc.kind == "call":
                rec_payload = rc.response[2] if rc.response and rc.response[0] == 3 else None
                self.rewriter.learn(rc.action, rec_payload if isinstance(rec_payload, dict) else None, new)
            self._entry(rc, replayed)
            if replayed["kind"] == "closed":
                break
            self.sent += 1

    async def _until_stopped(self, coro) -> Dict[str, Any]:
        """Await ``coro`` (a CALL); :meth:`stop` cancels it and the CALL counts as ``closed``."""
        task = asyncio.ensure_future(coro)
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                raise ConnectionError("replay stopped")
            return task.result()
        finally:
            stopper.cancel()
            if not task.done():
                task.cancel()

    def _entry(self, rc: RecordedCall, replayed: Dict[str, Any]) -> None:
        recorded = recorded_outcome(rc.response) if rc.kind == "call" else {"kind": "none"}
        match, diff = compare(recorded, replayed)
        self.entries.append({"index": rc.index, "seq": rc.seq, "action": rc.action, "recorded": recorded,
                             "replayed": replayed, "match": match, "diff": diff})

    def _report(self) -> Dict[str, Any]:
        done = {e["index"] for e in self.entries}
        for rc in self.plan.calls:
            if rc.index not in done:
                self._entry(rc, {"kind": "not_sent"})
        entries = sorted(self.entries, key=lambda e: e["index"])
        differed = sum(e["match"] is False for e in entries)
        notes = list(self.plan.notes) + list(self.rewriter.notes if self.rewriter else [])
        if self._inbound_total > len(self.inbound):
            notes.append(f"{self._inbound_total - len(self.inbound)} CSMS CALL(s) beyond the first {MAX_INBOUND} are "
                         "counted but not listed in inbound")
        if self.max_gap is not None:
            notes.append(f"waits between CALLs were capped at {self.max_gap} s")
        if self.plan.skipped:
            notes.append(f"{self.plan.skipped} unparseable charger frame(s) in the trace were not sent")
        ended = time.time()
        return {
            "format": FORMAT, "tool_version": __version__, "id": self.run_id, "trace": self.trace_name,
            "charger": self.plan.charger, "as": self.identity, "ocpp_version": self.plan.ocpp_version,
            "csms_url": fmt.strip_credentials(self._transport.csms_url),
            "security_profile": self._transport.security_profile, "timing": self.timing,
            "max_gap": self.max_gap, "session": self.session,
            "started": _iso(self._started or ended), "ended": _iso(ended),
            "wall_s": round(ended - (self._started or ended), 3),
            "result": "error" if self.error else ("diff" if differed else "match"), "error": self.error,
            "summary": {
                "calls": len(entries), "sent": self.sent,
                "matched": sum(e["match"] is True for e in entries), "differed": differed,
                "not_compared": sum(e["match"] is None for e in entries),
                "timeouts": sum(e["replayed"]["kind"] == "timeout" for e in entries),
                "not_sent": sum(e["replayed"]["kind"] == "not_sent" for e in entries),
                "inbound": self._inbound_total,
                "inbound_not_in_trace": self._inbound_unknown,
                "inbound_overflow": max(0, self._inbound_total - len(self.inbound)),
            },
            "calls": entries, "inbound": list(self.inbound), "notes": notes,
            "new_trace": str(self.out_trace) if self.out_trace else None,
        }


def exit_code(report: Dict[str, Any], *, fail_on_diff: bool = False) -> int:
    """0 = done (differences allowed unless ``fail_on_diff``), 1 = differences with ``fail_on_diff``, 2 = error."""
    if report.get("result") == "error":
        return 2
    return 1 if fail_on_diff and report.get("result") == "diff" else 0


def to_text(report: Dict[str, Any]) -> str:
    s = report["summary"]
    lines = [f"replay {report['id']}: {report['charger']} as {report['as']} (OCPP {report['ocpp_version']}) -> "
             f"{report['csms_url']}, timing {report['timing']}"
             + (f" (max gap {report['max_gap']} s)" if report.get("max_gap") else ""),
             f"result: {report['result']}" + (f" ({report['error']})" if report.get("error") else ""),
             f"CALLs: {s['calls']} sent {s['sent']}, matched {s['matched']}, differed {s['differed']}, "
             f"not compared {s['not_compared']}, timeouts {s['timeouts']}, not sent {s['not_sent']}",
             f"CSMS CALLs: {s['inbound']} ({s['inbound_not_in_trace']} not in the trace)"]
    for e in report["calls"]:
        if e["match"] is False:
            lines.append(f"  #{e['index']} {e['action']} (seq {e['seq']}): " + "; ".join(e["diff"]))
    for n in report.get("notes") or ():
        lines.append(f"note: {n}")
    if report.get("new_trace"):
        lines.append(f"new trace: {report['new_trace']}")
    return "\n".join(lines) + "\n"
