"""Single charger runtime (plan §2.2, M3 / M4b).

One :class:`ChargerRuntime` per charger owns the reconnect loop, the RPC connection, the version adapter, the
ConfigStore and the connector state machines. It speaks only the domain types of ``protocol.base`` and the
:class:`VersionAdapter` interface; it never sees version messages.

Task model: everything that needs the connection (boot / heartbeat loop, the outbox worker, meter sampling,
follow-ups of CSMS commands) runs as a *session task*. ``_spawn`` refuses new tasks once the session is no longer
connected, so cancelling the session tasks at the end of a session is race-free. Transactions survive a
disconnect; their meter loops restart after the next accepted boot.

Outbox (core spec §3.7 p.17-18): every transaction-related CP->CSMS message (StartTransaction, StopTransaction,
periodic MeterValues) and every StatusNotification caused by a state change goes through ONE ordered queue,
sent by ONE worker per session that runs once the boot is accepted:

* Enqueueing is synchronous. ``unplug`` / ``stop_tx`` / ``fault`` never wait for the CSMS, and a caller that is
  cancelled cannot lose a StopTransaction (it is queued before the caller can be interrupted). Only
  ``_start_tx`` waits, on a future of its StartTransaction item, because it needs the transactionId.
* The worker sends strictly in order and removes an item only once it is done. A lost connection is not a
  failure: the worker stops (and ends the session) and the item stays at the head for the next session. Any
  in-flight item whose answer was lost (StartTransaction, StopTransaction, MeterValues) is resent after the next
  accepted boot, so the CSMS may see it twice (§3.7 p.17; 1.6 has no idempotency key). A waiting
  StartTransaction caller gets ``Pending``. StatusNotification items are dropped at the end of a session because
  the state is reported afresh after the next accepted boot.
* A CSMS "failure to process" (CALLERROR, timeout, unparsable answer) of a transaction message is retried
  ``TransactionMessageAttempts`` times, waiting ``TransactionMessageRetryInterval`` x preceding transmissions
  before each retry (§3.7.1 p.17-18, §9.1.31 / §9.1.32 p.105). Any other exception is a local bug: it is logged
  and the item dropped. A failed StatusNotification is not retried. An exception while applying an answer
  (also a local bug) ends the session; a waiting StartTransaction caller gets ``Error``.
* The queue holds at most ``MAX_TX_QUEUE`` MeterValues; the oldest one is dropped first. StartTransaction /
  StopTransaction are never dropped *by this bound*; they are still dropped after ``TransactionMessageAttempts``
  failed attempts or an internal error.

Power model and smart charging (M4b, design §3.2 / §3.3): the runtime owns a :class:`ProfileStore`. While a
transaction is charging (StartTransaction answered), the connector's power model is stepped every ``tick_s`` and
before every meter reading / StopTransaction: ``composite.limit_at`` (the evaluator that also answers
GetCompositeSchedule, fed with the same per-phase voltage and phases) -> ``power.step`` -> power, energy register,
SoC. The suspension cause of the power model moves the connector Charging <-> SuspendedEV / SuspendedEVSE; the
StatusNotification goes through the outbox like any other state change. Every transaction starts a fresh EV at
``session.initial_soc``; when it ends the connector's TxProfiles are dropped (1.6 §3.13.1, §7.10). A
RemoteStartTransaction profile travels with the reservation / transaction and is installed, bound to the
transactionId, once StartTransaction is answered (§5.16.2); a start that is refused or fails discards it.

Timing: a SetChargingProfile / ClearChargingProfile first integrates every charging connector up to now at the old
limit; the new limit takes effect on the tick scheduled right after the CALLRESULT is sent (``_after_reply``),
not before the CSMS has the answer. ``time_scale`` scales only the power model (ramp, energy register, SoC);
profile evaluation always uses the wall clock (``clock``), because schedules are in CSMS time and must match what
GetCompositeSchedule reports. A Relative 60 s period therefore lasts 60 wall-clock seconds at any time_scale.

Interpretations (not defined by the spec): a limit in A applies to the phases the EV uses while one charges and to
the supply's phases otherwise (errata 3.34); GetCompositeSchedule periods are capped at the hardware maximum
(``max_power_w``, times the connector count for connectorId 0), which also stands for "no limit"; ticks are
session tasks, so after an offline gap the first step integrates the gap at the limit in force on reconnect.

Concurrency invariant: there is no lock around connector state. Every state check and the mutation that depends
on it run without an ``await`` in between (single-threaded asyncio), so they are atomic.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Coroutine, Deque, Dict, Iterable, List, Optional, Set, Tuple

from ..charging import power
from ..charging.composite import ActiveTransaction, CompositePeriod, composite_schedule, convert, limit_at
from ..charging.ev import PRESETS, EvModel
from ..charging.power import AcSupply, PowerState
from ..charging.profiles import ChargingProfile, ChargingRateUnit, ClearStatus, ProfileStatus, ProfileStore
from ..config import ChargerConfig, ReconnectConfig
from ..protocol import base as d
from ..protocol.base import AuthResult as A
from ..protocol.base import DomainStatus as S
from ..protocol.base import StopReason as R
from ..protocol.registry import get_adapter_class
from ..rpc import (Backoff, CallError, HandshakeError, RpcConnection, TransportConfig, open_connection, wait_event,
                   wait_or_stop)
from .config_store import ConfigStore
from .state import ConnectorState, IllegalTransition

log = logging.getLogger(__name__)

# Measurand names as used in the MeterValues*SampledData / StopTxn*Data configuration values.
ENERGY = "Energy.Active.Import.Register"
POWER = "Power.Active.Import"
CURRENT = "Current.Import"
VOLTAGE = "Voltage"
SOC = "SoC"
POWER_DECIMALS = 1  # same resolution as the GetCompositeSchedule limits
CURRENT_DECIMALS = 3
SOC_DECIMALS = 1
LOCATION = "Outlet"
SOC_LOCATION = "EV"

BACKOFF_FACTOR = 2.0
#: RpcConnection writes the CALLRESULT right after the handler returns; follow-up CALLs (StatusNotification,
#: StopTransaction, ...) wait this long so they go out behind it on the wire.
REPLY_DELAY_S = 0.01
#: How long a closed session waits for the receive loop to finish.
RX_DRAIN_TIMEOUT_S = 5.0
#: Maximum number of queued MeterValues; the oldest is dropped first.
MAX_TX_QUEUE = 1000

HEARTBEAT_KEY = "HeartbeatInterval"
SAMPLE_KEY = "MeterValueSampleInterval"
_INTERVAL_KEYS = (HEARTBEAT_KEY, SAMPLE_KEY)
CONNECTORS_KEY = "NumberOfConnectors"
SAMPLED_DATA_KEY = "MeterValuesSampledData"
STOP_SAMPLED_DATA_KEY = "StopTxnSampledData"
ATTEMPTS_KEY = "TransactionMessageAttempts"
RETRY_INTERVAL_KEY = "TransactionMessageRetryInterval"
STOP_ON_INVALID_KEY = "StopTransactionOnInvalidId"
AUTHORIZE_REMOTE_KEY = "AuthorizeRemoteTxRequests"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Stop:
    meter_stop_wh: int
    timestamp: datetime
    reason: R
    samples: List[d.Sample]


@dataclass
class _Tx:
    id_tag: str
    meter_start_wh: int
    started_at: datetime
    tx_id: Optional[str] = None    # set once the CSMS answered StartTransaction
    ended: Optional[_Stop] = None  # set when the transaction ended (maybe before tx_id arrived)
    profile: Optional[ChargingProfile] = None  # RemoteStart TxProfile, installed once tx_id is known


@dataclass
class _Conn:
    id: int
    state: ConnectorState
    plugged: bool = False
    operative: bool = True
    pending_operative: Optional[bool] = None  # ChangeAvailability "Scheduled"
    fault: Optional[str] = None               # version wire error code
    finishing: bool = False
    pending_tag: Optional[str] = None         # authorized, waiting for the cable
    pending_profile: Optional[ChargingProfile] = None  # RemoteStart TxProfile of ``pending_tag``
    tx: Optional[_Tx] = None
    meter_wh: float = 0.0                     # energy register
    pw: Optional[PowerState] = None           # power model state while charging (None otherwise)
    last_mono: Optional[float] = None
    meter_task: Optional[asyncio.Task] = None


class _Kind(Enum):
    START = "StartTransaction"
    STOP = "StopTransaction"
    METER = "MeterValues"
    STATUS = "StatusNotification"


class _Delivery(Enum):
    DONE = "done"        # delivered, or dropped (failure attempts exhausted / local bug)
    RETRY = "retry"      # failure to process; retry after the retry interval
    OFFLINE = "offline"  # connection lost; keep queued for the next session


@dataclass
class _Outcome:
    delivery: _Delivery
    value: Any = None  # the answer when delivered


@dataclass(eq=False)  # identity semantics: items are removed from the queue by identity
class _OutMsg:
    kind: _Kind
    send: Callable[[], Awaitable[Any]]
    conn: Optional[_Conn] = None
    tx: Optional[_Tx] = None
    result: Optional[asyncio.Future] = None  # StartTransaction: AuthResult for the waiting caller
    failures: int = 0

    @property
    def transactional(self) -> bool:
        """Transaction messages are retried, kept across sessions and counted as queued; StatusNotification is
        neither (the state is reported afresh after the next accepted boot)."""
        return self.kind is not _Kind.STATUS


class ChargerRuntime:
    def __init__(
        self,
        cfg: ChargerConfig,
        csms_url: str,
        ocpp_version: str = "1.6",
        *,
        reconnect: Optional[ReconnectConfig] = None,
        frame_log: Any = None,
        strict: bool = False,
        call_timeout: float = 30.0,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        connect: Callable[[TransportConfig], Awaitable[Any]] = open_connection,
        backoff: Optional[Backoff] = None,
        ev: Optional[EvModel] = None,
        tick_s: float = 1.0,
        time_scale: float = 1.0,
    ) -> None:
        """``ev`` defaults to the preset ``cfg.session.ev_model`` (``SimConfig.resolve_ev`` also knows
        ``ev_models``); ``tick_s`` is the power model step, ``time_scale`` its simulated-time factor. time_scale
        speeds up only the power model (ramp, energy, SoC); charging profiles stay on the wall clock (see the
        module docstring)."""
        if tick_s <= 0 or time_scale <= 0:
            raise ValueError("tick_s and time_scale must be > 0")
        if ev is None and cfg.session.ev_model not in PRESETS:
            raise ValueError(f"EV model {cfg.session.ev_model!r} is not a preset; pass ev=")
        self.cfg = cfg
        self.ev: EvModel = ev or PRESETS[cfg.session.ev_model]
        self.supply = AcSupply(cfg.voltage_v, cfg.phases, cfg.line_to_line)
        self.profiles = ProfileStore(cfg.connectors)
        self._tick_s, self._time_scale = tick_s, time_scale
        self.adapter: d.VersionAdapter = get_adapter_class(ocpp_version)(strict=strict)
        names = {k.name for k in self.adapter.config_keys}
        derived = {CONNECTORS_KEY: str(cfg.connectors)}
        if cfg.meter_sample_interval_s is not None:
            derived[SAMPLE_KEY] = str(cfg.meter_sample_interval_s)
        overrides = {k: v for k, v in derived.items() if k in names}
        overrides.update(cfg.config_keys)
        self.store = ConfigStore(self.adapter.config_keys, overrides)
        self.transport = TransportConfig(
            csms_url, cfg.id, subprotocol=self.adapter.subprotocol, password=cfg.password, ca_file=cfg.ca_file,
            verify_tls=cfg.verify_tls, require_subprotocol=cfg.require_subprotocol)
        rc = reconnect or ReconnectConfig()
        self._backoff = backoff or Backoff(rc.min_s, BACKOFF_FACTOR, rc.max_s, rc.jitter)
        self._frame_cb = frame_log.callback(cfg.id) if frame_log is not None else None
        self._call_timeout = call_timeout
        self._clock, self._mono, self._connect = clock, monotonic, connect

        self.station = ConnectorState(0)
        self._station_operative = True
        self._station_pending: Optional[bool] = None
        self.conns: Dict[int, _Conn] = {i: _Conn(i, ConnectorState(i)) for i in range(1, cfg.connectors + 1)}
        #: (vendorId, messageId or None) -> answer for inbound DataTransfer; default UnknownVendorId.
        self.data_transfer_rules: Dict[Tuple[str, Optional[str]], d.DataTransferResult] = {}

        self.connected = False
        self.registration: Optional[str] = None
        self.last_error: Optional[Dict[str, str]] = None
        self.connect_attempts = 0
        self.boots = 0
        self.clock_offset_s: Optional[float] = None
        self.last_heartbeat: Optional[datetime] = None
        self._tasks: Set[asyncio.Task] = set()
        self._boot_kick = asyncio.Event()
        self._reset_done = asyncio.Event()
        self._abort = asyncio.Event()  # a session task failed unexpectedly: drop the connection

        self._outq: Deque[_OutMsg] = deque()
        self._outq_wake = asyncio.Event()
        self._outq_idle = asyncio.Event()
        self._outq_idle.set()
        self._outq_busy: Optional[_OutMsg] = None  # head item the worker is waiting for / sending

        # One event per interval key, replaced on every change (broadcast wake-up for all waiting loops).
        self._wake: Dict[str, asyncio.Event] = {k: asyncio.Event() for k in _INTERVAL_KEYS}
        self.store.add_listener(self._on_config)

        self._commands: Dict[type, Callable[[Any], Any]] = {
            d.RequestStart: self._remote_start,
            d.RequestStop: self._remote_stop,
            d.GetConfig: self._get_config,
            d.SetConfig: lambda cmd: self.store.set(cmd.key, cmd.value),
            d.Reset: self._reset,
            d.Trigger: self._trigger,
            d.Unlock: self._unlock,
            d.ChangeAvailability: self._change_availability,
            d.DataTransfer: self._data_transfer,
            d.SetProfile: self._set_profile,
            d.ClearProfiles: self._clear_profiles,
            d.GetComposite: self._get_composite,
        }

    # ================================================================== connection

    @property
    def connector_count(self) -> int:
        return len(self.conns)

    @property
    def online(self) -> bool:
        return self.connected and self.registration == d.BootStatus.ACCEPTED.value

    async def run(self, stop: asyncio.Event) -> None:
        """Connect, boot, serve; reconnect with back-off until ``stop`` is set.

        Setting ``stop`` while a connection attempt is in progress may take up to the transport's
        ``open_timeout`` to return.
        """
        while not stop.is_set():
            self.connect_attempts += 1
            try:
                ws = await self._connect(self.transport)
            except HandshakeError as e:
                self.last_error = {"kind": e.kind, "message": str(e)}
                log.warning("%s: connect failed (%s)", self.cfg.id, e.kind)
                if await wait_or_stop(stop, self._backoff.next()):
                    break
                continue
            # The back-off is reset only after an accepted boot (see _boot): a CSMS that accepts the socket and
            # then drops it must still be backed off.
            was_reset = await self._session(ws, stop)
            if stop.is_set():
                break
            if not was_reset and await wait_or_stop(stop, self._backoff.next()):
                break

    async def _session(self, ws: Any, stop: asyncio.Event) -> bool:
        """Serve one connection; returns True when it ended because of a Reset."""
        self._reset_done.clear()
        self._abort.clear()
        self.registration = None
        rpc = RpcConnection(ws, self.adapter.handlers(self), error_codes=self.adapter.error_codes,
                            call_timeout=self._call_timeout, on_frame=self._frame_cb, name=self.cfg.id)
        self.adapter.bind(rpc.call)
        self.connected = True
        rx = asyncio.create_task(rpc.run())
        waits = [asyncio.create_task(stop.wait()), asyncio.create_task(self._reset_done.wait()),
                 asyncio.create_task(self._abort.wait())]
        self._spawn(self._online_guarded())
        try:
            await asyncio.wait([rx, *waits], return_when=asyncio.FIRST_COMPLETED)
        finally:
            rx_error = await self._close_session(rpc, rx, waits)
        # An abort that recorded no error of its own (the outbox worker lost the connection) is a disconnect.
        aborted = self._abort.is_set() and self.last_error is not None
        if not self._reset_done.is_set() and not stop.is_set() and not aborted:
            self.last_error = {"kind": "disconnected", "message": self._close_text(ws, rx_error)}
        return self._reset_done.is_set()

    async def _close_session(self, rpc: RpcConnection, rx: asyncio.Task, waits: List[asyncio.Task]) -> Optional[str]:
        """Tear one session down; returns why the receive loop failed, if it did."""
        self.connected = False  # from here on _spawn refuses: the task set below is final
        self.adapter.bind(None)
        for w in waits:
            w.cancel()
        await self._cancel_tasks()
        self._outq_session_ended()
        await rpc.close()
        try:
            async with asyncio.timeout(RX_DRAIN_TIMEOUT_S):
                await rx
        except TimeoutError:
            rx.cancel()
            await asyncio.gather(rx, return_exceptions=True)
            log.warning("%s: receive loop did not finish", self.cfg.id)
            return "receive loop did not finish"
        except Exception as e:  # noqa: BLE001 - record why the connection died
            log.warning("%s: receive loop failed: %r", self.cfg.id, e)
            return repr(e)
        return None

    @staticmethod
    def _close_text(ws: Any, rx_error: Optional[str]) -> str:
        code, reason = getattr(ws, "close_code", None), getattr(ws, "close_reason", None)
        text = "connection closed"
        if code is not None:
            text += f" (code {code}{': ' + reason if reason else ''})"
        return f"{text}; {rx_error}" if rx_error else text

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> Optional[asyncio.Task]:
        """Start a session task; refused (``coro`` closed, None returned) when the session is not connected."""
        if not self.connected:
            log.debug("%s: session ended, task not started", self.cfg.id)
            coro.close()
            return None
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("%s: task failed", self.cfg.id, exc_info=t.exception())

    async def _cancel_tasks(self) -> None:
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for c in self.conns.values():
            c.meter_task = None

    def _after_reply(self, fn: Callable[[], Any]) -> None:
        """Call ``fn`` (awaiting its result when it is awaitable) after the CALLRESULT of the current CSMS CALL
        went out. Bind arguments now (``functools.partial``): state read at call time may have changed."""
        async def later() -> None:
            await asyncio.sleep(REPLY_DELAY_S)
            res = fn()
            if inspect.isawaitable(res):
                await res
        self._spawn(later())

    async def _safe(self, aw: Awaitable[Any], what: str) -> Any:
        try:
            return await aw
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a failed CP->CSMS message must not kill the runtime
            log.warning("%s: %s failed: %r", self.cfg.id, what, e)
            return None

    # ================================================================== configuration changes

    def _on_config(self, key: str, value: str) -> None:
        ev = self._wake.get(key)
        if ev is not None:
            ev.set()
            self._wake[key] = asyncio.Event()

    async def _wait_interval(self, key: str, interval: int) -> bool:
        """Wait ``interval`` seconds (until changed when 0 = disabled). True when ``key`` changed meanwhile."""
        return await wait_event(self._wake[key], interval if interval > 0 else None)

    # ================================================================== boot / heartbeat

    async def _online_guarded(self) -> None:
        try:
            await self._online()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - no zombie session: let the reconnect loop take over
            log.exception("%s: session failed", self.cfg.id)
            self.last_error = {"kind": "internal", "message": repr(e)}
            self._abort.set()

    async def _online(self) -> None:
        await self._boot()
        # Bring every state up to date first (a transaction may have ended while offline), then report.
        self._apply(self.station, self._station_target())
        for c in self.conns.values():
            self._apply(c.state, self._target(c), c.fault)
        await self._send_status(self.station)
        for c in self.conns.values():
            await self._send_status(c.state)
        self._spawn(self._outbox_worker())
        self._spawn(self._tick_loop())
        for c in self.conns.values():
            if c.tx is not None and c.tx.tx_id is not None:
                self._start_meter(c)
        while True:
            if not await self._wait_interval(HEARTBEAT_KEY, self.store.get_int(HEARTBEAT_KEY)):
                await self._heartbeat()

    async def _boot(self) -> None:
        """BootNotification until Accepted; CSMS CALLs are served meanwhile by the RPC layer."""
        while True:
            self._boot_kick.clear()
            delay: float = self.cfg.boot_retry_s
            res = await self._send_boot()
            if res is not None:
                if res.status is d.BootStatus.ACCEPTED:
                    self._backoff.reset()
                    self.last_error = None
                    return
                if res.interval > 0:
                    delay = res.interval
            await wait_event(self._boot_kick, delay)

    async def _send_boot(self, *, triggered: bool = False) -> Optional[d.BootResult]:
        ev = d.Booting(self.cfg.vendor, self.cfg.model, self.cfg.serial, self.cfg.firmware)
        res: Optional[d.BootResult] = await self._safe(self.adapter.send_booting(ev), "BootNotification")
        if res is None:
            return None
        self.boots += 1
        self._note_time(res.current_time)
        if triggered:
            # TODO(spec): §5.17 TriggerMessage does not say what the answer to a triggered BootNotification
            # changes once the charger is registered; the simulator keeps its registration (a Pending / Rejected
            # answer would otherwise take an accepted charger offline with no boot loop running) and only logs it.
            log.info("%s: triggered BootNotification answered %s (registration kept)", self.cfg.id,
                     res.status.value)
        else:
            self.registration = res.status.value
        if res.status is d.BootStatus.ACCEPTED and res.interval > 0:
            self.store.set(HEARTBEAT_KEY, str(res.interval))
        return res

    async def _heartbeat(self) -> None:
        t = await self._safe(self.adapter.send_heartbeat(d.Heartbeat()), "Heartbeat")
        if t is not None:
            self._note_time(t)
            self.last_heartbeat = self._clock()

    def _note_time(self, t: Optional[datetime]) -> None:
        if t is None:
            return
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        self.clock_offset_s = (t - self._clock()).total_seconds()

    # ================================================================== outbox (ordered CP->CSMS queue)

    def _outq_push(self, msg: _OutMsg, *, front: bool = False) -> None:
        """Queue ``msg`` (synchronous: never loses a message to a cancelled caller)."""
        if msg.kind is _Kind.METER:
            meters = [m for m in self._outq if m.kind is _Kind.METER and m is not self._outq_busy]
            if len(meters) >= MAX_TX_QUEUE:
                self._outq.remove(meters[0])
                log.warning("%s: outbox full (%d MeterValues), oldest MeterValues dropped", self.cfg.id,
                            MAX_TX_QUEUE)
        if front:
            self._outq.appendleft(msg)
        else:
            self._outq.append(msg)
        self._outq_idle.clear()
        self._outq_wake.set()

    def _outq_remove(self, msg: _OutMsg) -> None:
        # Idle is set only by the worker, once applying the answer (which may queue a StopTransaction) is done.
        if msg in self._outq:
            self._outq.remove(msg)

    def _outq_session_ended(self) -> None:
        """Drop stale StatusNotifications; tell StartTransaction callers that the answer is pending."""
        # TODO(spec): any in-flight item whose answer was lost with the connection (StartTransaction,
        # StopTransaction, MeterValues) stays queued and is resent after the next accepted boot (§3.7 p.17); 1.6
        # has no idempotency key, so the CSMS may see it twice.
        for msg in list(self._outq):
            if not msg.transactional:
                self._outq.remove(msg)
            elif msg.result is not None and not msg.result.done():
                msg.result.set_result(A.PENDING)
        if not self._outq:
            self._outq_idle.set()

    async def _outbox_worker(self) -> None:
        """Session task: send queued messages strictly in order; stops (head kept) when the connection is lost."""
        while True:
            while self._outq:
                msg = self._outq[0]
                self._outq_busy = msg
                try:
                    if msg.failures:
                        await asyncio.sleep(self.store.get_int(RETRY_INTERVAL_KEY) * msg.failures)
                    out = await self._deliver(msg)
                finally:
                    self._outq_busy = None
                if out.delivery is _Delivery.OFFLINE:
                    # End the session: a stalled outbox must never coexist with a live session. The next
                    # accepted boot starts a new worker.
                    self._abort.set()
                    return
                if out.delivery is _Delivery.DONE:
                    self._outq_remove(msg)
                    if not self._delivered_guarded(msg, out.value):
                        return
            # No await between the empty check above and set() / clear(): a later push clears idle and wakes the
            # wait.
            self._outq_idle.set()
            self._outq_wake.clear()
            await self._outq_wake.wait()

    async def _deliver(self, msg: _OutMsg) -> _Outcome:
        what = msg.kind.value
        try:
            return _Outcome(_Delivery.DONE, await msg.send())
        except asyncio.CancelledError:
            raise
        except ConnectionError as e:
            log.info("%s: %s kept queued (%r)", self.cfg.id, what, e)
            return _Outcome(_Delivery.OFFLINE)
        except (CallError, TimeoutError, ValueError) as e:
            # Failure to process: CALLERROR, no answer in time, unparsable / invalid answer (§3.7.1 p.17-18).
            if not msg.transactional:
                log.warning("%s: %s failed: %r", self.cfg.id, what, e)
                return _Outcome(_Delivery.DONE)
            msg.failures += 1
            attempts = max(1, self.store.get_int(ATTEMPTS_KEY))
            if msg.failures >= attempts:
                log.warning("%s: %s discarded after %d failed attempts: %r", self.cfg.id, what, msg.failures, e)
                return _Outcome(_Delivery.DONE)
            log.warning("%s: %s failed (%d/%d): %r", self.cfg.id, what, msg.failures, attempts, e)
            return _Outcome(_Delivery.RETRY)
        except Exception:  # noqa: BLE001 - a local bug, not the CSMS: drop the item, keep the worker alive
            log.exception("%s: %s dropped (internal error)", self.cfg.id, what)
            return _Outcome(_Delivery.DONE)

    def _delivered_guarded(self, msg: _OutMsg, value: Any) -> bool:
        """Apply an answer; on a local bug end the session (no zombie worker) and return False."""
        try:
            self._delivered(msg, value)
            return True
        except Exception as e:  # noqa: BLE001 - the item is already removed: fail loudly, restart the session
            log.exception("%s: applying the %s answer failed", self.cfg.id, msg.kind.value)
            self.last_error = {"kind": "internal", "message": repr(e)}
            if msg.result is not None and not msg.result.done():
                msg.result.set_result(A.ERROR)
            if msg.kind is _Kind.START and msg.conn is not None and msg.tx is not None and msg.tx.tx_id is None:
                try:  # the answer was not applied: do not leave the connector stuck in Preparing
                    self._abort_tx(msg.conn, msg.tx, finishing=False)
                except Exception:  # noqa: BLE001
                    log.exception("%s: detaching the failed transaction failed", self.cfg.id)
            self._abort.set()
            return False

    def _delivered(self, msg: _OutMsg, value: Any) -> None:
        if msg.kind is _Kind.START:
            assert msg.conn is not None and msg.tx is not None
            outcome = self._on_started(msg.conn, msg.tx, value)
            if msg.result is not None and not msg.result.done():
                msg.result.set_result(outcome)

    # ================================================================== status

    def _target(self, c: _Conn) -> S:
        if c.fault:
            return S.FAULTED
        if not c.operative:
            return S.UNAVAILABLE
        if c.tx is not None:
            # Charging only once the CSMS answered StartTransaction; then the power model may suspend it.
            if c.tx.tx_id is None:
                return S.PREPARING
            return S(c.pw.suspension) if c.pw is not None and c.pw.suspension else S.CHARGING
        if c.plugged and c.finishing:
            return S.FINISHING
        if c.plugged or c.pending_tag:
            return S.PREPARING
        return S.AVAILABLE

    def _station_target(self) -> S:
        return S.AVAILABLE if self._station_operative else S.UNAVAILABLE

    def _apply(self, st: ConnectorState, new: S, error_code: Optional[str] = None) -> bool:
        try:
            return st.transition(new, error_code=error_code)
        except IllegalTransition as e:
            # TODO(spec): a transition outside the (unverified) 1.6 table is forced instead of refused, so the
            # simulator never gets stuck; it is logged for review.
            log.warning("%s: %s (forced)", self.cfg.id, e)
            st.force(new, error_code=error_code)
            return True

    def _refresh(self, c: _Conn) -> None:
        """Move ``c`` to its target state; when online, queue the StatusNotification (never waits)."""
        if self._apply(c.state, self._target(c), c.fault) and self.online:
            self._queue_status(c.state)

    def _refresh_station(self) -> None:
        if self._apply(self.station, self._station_target()) and self.online:
            self._queue_status(self.station)

    def _status_event(self, st: ConnectorState) -> d.ConnectorStatus:
        return d.ConnectorStatus(st.connector_id, st.connector_id, st.status, st.error_code, st.info,
                                 st.vendor_error_code, self._clock())

    def _queue_status(self, st: ConnectorState) -> None:
        ev = self._status_event(st)  # captured now: the state may change before it is sent
        self._outq_push(_OutMsg(_Kind.STATUS, lambda: self.adapter.send_connector_status(ev)))

    async def _send_status(self, st: ConnectorState) -> None:
        await self._safe(self.adapter.send_connector_status(self._status_event(st)), "StatusNotification")

    # ================================================================== authorization / transactions

    async def _authorize(self, tag: str) -> A:
        mode = self.cfg.auth_mode
        if mode == "accept_all":
            return A.ACCEPTED
        local = tag in self.cfg.id_tags
        if mode == "local_only":
            return A.ACCEPTED if local else A.INVALID
        if mode == "local_then_csms" and local:
            return A.ACCEPTED
        if not self.online:
            return A.OFFLINE  # TODO(spec): offline authorization (LocalAuthorizeOffline / cache) is not simulated
        info: Optional[d.IdTokenInfo] = await self._safe(self.adapter.send_authorize(d.AuthorizeRequest(tag)),
                                                         "Authorize")
        return info.status if info is not None else A.ERROR

    def _samples(self, c: _Conn, measurands: Iterable[str]) -> List[d.Sample]:
        """Readings of the configured measurands (others are skipped; SoC only while an EV charges).
        Current.Import is the balanced per-phase current, reported once without a phase."""
        r = power.meter_reading(c.pw, self.ev, self.supply) if c.pw is not None else None
        watts = r.power_active_import_w if r else 0.0
        amps = r.current_import_a if r else 0.0
        out: List[d.Sample] = []
        for name in measurands:
            if name == ENERGY:
                out.append(d.Sample(name, round(c.meter_wh), "Wh", location=LOCATION))
            elif name == POWER:
                out.append(d.Sample(name, round(watts, POWER_DECIMALS), "W", location=LOCATION))
            elif name == CURRENT:
                out.append(d.Sample(name, round(amps, CURRENT_DECIMALS), "A", location=LOCATION))
            elif name == VOLTAGE:
                out.append(d.Sample(name, self.supply.voltage_v, "V", location=LOCATION))
            elif name == SOC and r is not None:
                out.append(d.Sample(name, round(r.soc_pct, SOC_DECIMALS), "Percent", location=SOC_LOCATION))
        return out

    # ================================================================== power model / smart charging

    def _phases(self, connector_id: int) -> int:
        """Phases a limit in A applies to: the EV's while one charges, else the supply's (errata 3.34)."""
        # A charging EV draws on min(EV, supply) phases, so an A limit only reaches those (errata 3.34); with no EV
        # (or connector 0, the whole charger) the limit applies to every supply phase.
        c = self.conns.get(connector_id)
        return self.supply.phases_used(self.ev) if c is not None and c.pw is not None else self.supply.phases

    def _transactions(self) -> Dict[int, ActiveTransaction]:
        # TODO(spec): 1.6 transaction ids are integers; 2.0.1 string ids need a mapping in charging.profiles.
        return {c.id: ActiveTransaction(int(c.tx.tx_id), c.tx.started_at)
                for c in self.conns.values() if c.tx is not None and c.tx.tx_id is not None}

    def _limit_w(self, c: _Conn) -> Optional[float]:
        return limit_at(self.profiles, c.id, self._clock(), transactions=self._transactions(),
                        number_phases=self._phases(c.id), voltage=self.supply.phase_voltage_v())

    def _advance(self, c: _Conn) -> None:
        """Step the power model of a charging connector up to now (power, energy register, SoC, suspension) and
        restart the interval; a connector that is not charging only restarts the interval."""
        now = self._mono()
        if c.pw is not None:
            dt = max(0.0, now - c.last_mono) if c.last_mono is not None else 0.0
            c.pw = power.step(c.pw, dt_s=dt, ev=self.ev, target_soc_pct=self.cfg.session.target_soc,
                              evse_limit_w=self._limit_w(c), evse_max_w=self.cfg.max_power_w, supply=self.supply,
                              ramp_up_w_per_s=self.cfg.ramp_up_w_per_s,
                              ramp_down_w_per_s=self.cfg.ramp_down_w_per_s, time_scale=self._time_scale)
            c.meter_wh = c.pw.energy_wh
        c.last_mono = now

    def _settle(self) -> None:
        """Integrate every charging connector up to now (before a limit change)."""
        for c in self.conns.values():
            self._advance(c)

    def _tick_all(self) -> None:
        for c in self.conns.values():
            if c.pw is not None:
                self._advance(c)
                self._refresh(c)  # Charging <-> SuspendedEV / SuspendedEVSE

    async def _tick_loop(self) -> None:
        """Session task: step the power model of every charging connector each ``tick_s``."""
        while True:
            await asyncio.sleep(self._tick_s)
            try:
                self._tick_all()
            except Exception:  # a local bug must not stop the power model for the rest of the session
                log.exception("%s: power model tick failed", self.cfg.id)

    def _bind_profile(self, c: _Conn, tx: _Tx) -> None:
        """Install the RemoteStart TxProfile for the transaction it started (§5.16.2)."""
        assert tx.profile is not None and tx.tx_id is not None
        tx_id = int(tx.tx_id)
        st = self._install(c.id, replace(tx.profile, transaction_id=tx_id), active_transaction=tx_id)
        if st is not ProfileStatus.ACCEPTED:
            log.warning("%s: RemoteStart charging profile not installed (%s)", self.cfg.id, st.value)

    def _install(self, connector_id: int, profile: ChargingProfile, *,
                 active_transaction: Optional[int]) -> ProfileStatus:
        """ProfileStore.install, logging a same-id profile it replaces on another connector (3.13.2)."""
        moved = [ip.connector_id for ip in self.profiles.profiles()
                 if ip.profile.profile_id == profile.profile_id and ip.connector_id != connector_id]
        st = self.profiles.install(connector_id, profile, active_transaction=active_transaction)
        if st is ProfileStatus.ACCEPTED and moved:
            log.info("%s: charging profile %d on connector %s replaced by the one for connector %d", self.cfg.id,
                     profile.profile_id, ",".join(map(str, moved)), connector_id)
        return st

    async def _start_tx(self, c: _Conn, tag: str, profile: Optional[ChargingProfile] = None) -> A:
        """Start a transaction for an authorized ``tag``; the state is re-checked here (an await may lie between
        authorization and this call). Not plugged = keep the tag and wait for the cable (Accepted).

        No await until the StartTransaction item is queued: checks and changes below are atomic."""
        if c.tx is not None:
            return A.BUSY
        if c.fault or not c.operative:
            return A.UNAVAILABLE
        c.finishing = False
        if not c.plugged:
            c.pending_tag, c.pending_profile = tag, profile
            self._refresh(c)
            return A.ACCEPTED
        c.pending_tag, c.pending_profile = None, None
        if not self.online:
            # TODO(spec): offline transactions are not simulated in v0.1 (§3.7 p.17 would queue a
            # StartTransaction without transactionId); the start is refused with Offline.
            self._refresh(c)
            return A.OFFLINE
        tx = c.tx = _Tx(tag, int(round(c.meter_wh)), self._clock(), profile=profile)
        c.last_mono = self._mono()
        self._refresh(c)  # Preparing
        ev = d.TxStarted(c.id, tx.id_tag, tx.meter_start_wh, tx.started_at)
        result: asyncio.Future = asyncio.get_running_loop().create_future()
        self._outq_push(_OutMsg(_Kind.START, lambda: self.adapter.send_tx_started(ev), c, tx, result))
        # The caller may be cancelled here: the item stays queued and the worker finishes the start.
        return await result

    def _on_started(self, c: _Conn, tx: _Tx, res: Optional[d.TxStartedResult]) -> A:
        """Apply the StartTransaction answer (worker context, synchronous); returns the caller's outcome."""
        if res is None:  # discarded after TransactionMessageAttempts failures, or dropped
            self._abort_tx(c, tx, finishing=False)
            return A.ERROR
        if c.tx is not tx:
            # unplug / stop / fault / Reset while StartTransaction was in flight: the CSMS now knows the
            # transaction, so its StopTransaction goes out next.
            tx.tx_id = res.tx_id
            if tx.ended is not None:
                self._outq_push(self._stop_msg(c, tx), front=True)
            return A.ACCEPTED
        tx.tx_id = res.tx_id
        if not res.id_token_info.accepted and self.store.get_bool(STOP_ON_INVALID_KEY):
            # TODO(spec): MaxEnergyOnInvalidId / suspending instead of stopping is not simulated.
            self._end_tx_now(c, R.DE_AUTHORIZED, tx)
            return res.id_token_info.status
        if tx.profile is not None:
            self._bind_profile(c, tx)
        c.pw = PowerState(soc_pct=self.cfg.session.initial_soc, energy_wh=c.meter_wh)  # a fresh EV session
        c.last_mono = None
        self._advance(c)  # zero-length step: initial power and suspension; charging starts now
        self._refresh(c)  # Charging (or Suspended*)
        self._start_meter(c)
        return A.ACCEPTED

    def _start_meter(self, c: _Conn) -> None:
        if c.meter_task is None or c.meter_task.done():
            c.meter_task = self._spawn(self._meter_loop(c))

    async def _meter_loop(self, c: _Conn) -> None:
        # TODO(spec): clock-aligned readings (ClockAlignedDataInterval / MeterValuesAlignedData) are not sent yet.
        while c.tx is not None:
            if await self._wait_interval(SAMPLE_KEY, self.store.get_int(SAMPLE_KEY)):
                continue  # interval changed: restart the wait with the new value
            tx = c.tx
            if tx is None or tx.tx_id is None:
                continue
            self._advance(c)
            ev = d.TxUpdated(c.id, tx.tx_id, self._clock(), self._samples(c, self.store.get_csl(SAMPLED_DATA_KEY)))
            self._outq_push(_OutMsg(_Kind.METER, lambda ev=ev: self.adapter.send_tx_updated(ev), c, tx))

    def _end_tx(self, c: _Conn, reason: R, *, tx: Optional[_Tx] = None) -> None:
        """End the running transaction (only if it is ``tx`` when given). Never waits for the CSMS."""
        cur = c.tx
        if cur is not None and (tx is None or cur is tx):
            self._end_tx_now(c, reason, cur)

    def _end_tx_now(self, c: _Conn, reason: R, tx: _Tx) -> None:
        self._advance(c)
        tx.ended = _Stop(int(round(c.meter_wh)), self._clock(), reason,
                         self._samples(c, self.store.get_csl(STOP_SAMPLED_DATA_KEY)))
        if tx.tx_id is not None:
            # TODO(spec): StopTransaction is sent before the Finishing / Available StatusNotification.
            self._outq_push(self._stop_msg(c, tx))
        # else: StartTransaction is still queued; its answer queues the StopTransaction (_on_started).
        self._abort_tx(c, tx, finishing=c.plugged)

    def _abort_tx(self, c: _Conn, tx: _Tx, *, finishing: bool) -> None:
        """Detach ``tx`` from ``c`` (no-op when it is no longer current), apply scheduled availability changes
        and refresh the states. Shared by a normal end and a failed start."""
        if c.tx is not tx:
            return
        if c.meter_task is not None and c.meter_task is not asyncio.current_task():
            c.meter_task.cancel()
        c.meter_task = None
        c.tx, c.last_mono, c.finishing, c.pw = None, None, finishing, None
        self.profiles.end_transaction(c.id)
        if c.pending_operative is not None:
            c.operative, c.pending_operative = c.pending_operative, None
        station = self._station_pending is not None and all(x.tx is None for x in self.conns.values())
        if station:
            self._station_operative, self._station_pending = self._station_pending, None
        self._refresh(c)
        if station:
            self._refresh_station()

    def _stop_msg(self, c: _Conn, tx: _Tx) -> _OutMsg:
        st = tx.ended
        assert st is not None and tx.tx_id is not None
        ev = d.TxEnded(c.id, tx.tx_id, st.meter_stop_wh, st.timestamp, st.reason, tx.id_tag, st.samples)
        return _OutMsg(_Kind.STOP, lambda: self.adapter.send_tx_ended(ev), c, tx)

    # ================================================================== CSMS commands

    async def handle(self, cmd: d.Command) -> Any:
        fn = self._commands.get(type(cmd))
        return fn(cmd) if fn is not None else d.CommandStatus.NOT_IMPLEMENTED

    def _set_profile(self, cmd: d.SetProfile) -> d.CommandStatus:
        c = self.conns.get(cmd.evse_id)
        active = int(c.tx.tx_id) if c is not None and c.tx is not None and c.tx.tx_id is not None else None
        self._settle()
        if self._install(cmd.evse_id, cmd.profile, active_transaction=active) is not ProfileStatus.ACCEPTED:
            return d.CommandStatus.REJECTED
        self._after_reply(self._tick_all)
        return d.CommandStatus.ACCEPTED

    def _clear_profiles(self, cmd: d.ClearProfiles) -> d.CommandStatus:
        self._settle()
        st = self.profiles.clear(profile_id=cmd.profile_id, connector_id=cmd.evse_id, purpose=cmd.purpose,
                                 stack_level=cmd.stack_level)
        if st is not ClearStatus.ACCEPTED:
            return d.CommandStatus.UNKNOWN
        self._after_reply(self._tick_all)
        return d.CommandStatus.ACCEPTED

    def _get_composite(self, cmd: d.GetComposite) -> d.GetCompositeResult:
        if not 0 <= cmd.evse_id <= self.connector_count or cmd.duration < 1:
            return d.GetCompositeResult(d.CommandStatus.REJECTED)
        now = self._clock()
        phases, volts = self._phases(cmd.evse_id), self.supply.phase_voltage_v()
        sched = composite_schedule(self.profiles, cmd.evse_id, now, cmd.duration, cmd.unit,
                                   transactions=self._transactions(), number_phases=phases, voltage=volts)
        max_w = self.cfg.max_power_w * (self.connector_count if cmd.evse_id == 0 else 1)

        def cap(p: CompositePeriod) -> CompositePeriod:
            # The hardware maximum caps every period and stands for "no limit" (see the module docstring).
            if sched.unit is ChargingRateUnit.W:
                return replace(p, limit=max_w if p.limit is None else min(p.limit, max_w))
            n = p.number_phases or phases
            hw = convert(max_w, ChargingRateUnit.W, ChargingRateUnit.A, n, volts)
            return replace(p, limit=hw if p.limit is None else min(p.limit, hw), number_phases=n)

        return d.GetCompositeResult(d.CommandStatus.ACCEPTED, cmd.evse_id, now,
                                    replace(sched, periods=tuple(cap(p) for p in sched.periods)))

    def _get_config(self, cmd: d.GetConfig) -> d.GetConfigResult:
        known, unknown = self.store.get(cmd.keys)
        return d.GetConfigResult(known, unknown)

    def _reset(self, cmd: d.Reset) -> d.CommandStatus:
        self._after_reply(functools.partial(self._do_reset, cmd.kind))
        return d.CommandStatus.ACCEPTED

    def _unlock(self, cmd: d.Unlock) -> d.UnlockStatus:
        c = self.conns[cmd.evse_id]
        if c.tx is not None:
            self._after_reply(functools.partial(self._end_tx, c, R.UNLOCK_COMMAND, tx=c.tx))
        return d.UnlockStatus.UNLOCKED  # TODO(spec): UnlockFailed / NotSupported answers are not configurable yet

    def _data_transfer(self, cmd: d.DataTransfer) -> d.DataTransferResult:
        rule = (self.data_transfer_rules.get((cmd.vendor_id, cmd.message_id))
                or self.data_transfer_rules.get((cmd.vendor_id, None)))
        return rule or d.DataTransferResult(d.DataTransferStatus.UNKNOWN_VENDOR_ID)

    def _remote_start(self, cmd: d.RequestStart) -> d.CommandStatus:
        if not self.online:
            return d.CommandStatus.REJECTED  # TODO(spec): RemoteStart before an accepted boot is rejected

        def free(c: _Conn) -> bool:
            return c.tx is None and c.operative and not c.fault and c.pending_tag is None

        if cmd.evse_id is None:
            # TODO(spec): without connectorId the first free connector is used.
            c = next((x for x in self.conns.values() if free(x)), None)
        else:
            c = self.conns[cmd.evse_id]
        if c is None or not free(c):
            return d.CommandStatus.REJECTED
        if cmd.profile is not None and self.profiles.validate(
                c.id, cmd.profile, for_remote_start=True) is not ProfileStatus.ACCEPTED:
            return d.CommandStatus.REJECTED
        # Reserve until the follow-up runs; the profile travels with the tag, so a cable plugged in meanwhile
        # (plug -> _start_tx) starts the transaction with it (§5.16.2).
        c.pending_tag, c.pending_profile = cmd.id_token, cmd.profile
        self._after_reply(functools.partial(self._remote_start_go, c, cmd.id_token, cmd.profile))
        return d.CommandStatus.ACCEPTED

    async def _remote_start_go(self, c: _Conn, tag: str, profile: Optional[ChargingProfile] = None) -> None:
        if self.store.get_bool(AUTHORIZE_REMOTE_KEY) and await self._authorize(tag) is not A.ACCEPTED:
            if c.pending_tag == tag:  # still our reservation (the await may have let another start in)
                c.pending_tag, c.pending_profile = None, None
                self._refresh(c)
            return
        # TODO(spec): ConnectionTimeOut (cable not plugged in time) is not enforced.
        await self._start_tx(c, tag, profile)

    def _remote_stop(self, cmd: d.RequestStop) -> d.CommandStatus:
        for c in self.conns.values():
            if c.tx is not None and c.tx.tx_id == cmd.tx_id:
                self._after_reply(functools.partial(self._end_tx, c, R.REMOTE, tx=c.tx))
                return d.CommandStatus.ACCEPTED
        return d.CommandStatus.REJECTED

    async def _do_reset(self, kind: d.ResetKind) -> None:
        # TODO(spec): both Soft and Hard reset stop running transactions before closing (reason SoftReset /
        # HardReset); a real Hard reset may send StopTransaction only after the reboot.
        try:
            reason = R.SOFT_RESET if kind is d.ResetKind.SOFT else R.HARD_RESET
            for c in self.conns.values():
                self._end_tx(c, reason)
                c.pending_tag, c.pending_profile = None, None
            # Let the StopTransactions go out before closing, including one queued by a StartTransaction answer
            # that arrives meanwhile; bounded, a silent CSMS must not block the Reset (anything left stays queued
            # for the next session). Idle is set only when the queue is empty and no answer is being applied.
            try:
                async with asyncio.timeout(self._call_timeout):
                    while self._outq:
                        await self._outq_idle.wait()
            except TimeoutError:
                log.info("%s: Reset closes with %d queued message(s)", self.cfg.id, len(self._outq))
        finally:
            self._reset_done.set()

    def _trigger(self, cmd: d.Trigger) -> d.CommandStatus:
        what = cmd.what
        if what is d.TriggerWhat.BOOT:
            if self.registration != d.BootStatus.ACCEPTED.value:
                self._boot_kick.set()
            else:
                self._after_reply(functools.partial(self._send_boot, triggered=True))
        elif what is d.TriggerWhat.HEARTBEAT:
            self._after_reply(self._heartbeat)
        elif what is d.TriggerWhat.STATUS:
            targets = ([self.station] if cmd.evse_id in (None, 0) else []) + [
                c.state for c in self.conns.values() if cmd.evse_id in (None, c.id)]
            self._after_reply(functools.partial(self._send_statuses, targets))
        elif what is d.TriggerWhat.METER_VALUES:
            # TODO(spec): connectorId 0 / absent triggers a reading per connector (no main meter is simulated).
            targets = [c for c in self.conns.values() if cmd.evse_id in (None, 0, c.id)]
            self._after_reply(functools.partial(self._send_readings, targets))
        else:
            return d.CommandStatus.NOT_IMPLEMENTED
        return d.CommandStatus.ACCEPTED

    async def _send_statuses(self, states: List[ConnectorState]) -> None:
        for st in states:
            await self._send_status(st)

    async def _send_readings(self, conns: List[_Conn]) -> None:
        for c in conns:
            self._advance(c)
            tx_id = c.tx.tx_id if c.tx is not None else None
            ev = d.MeterSample(c.id, self._clock(), self._samples(c, self.store.get_csl(SAMPLED_DATA_KEY)),
                               "Trigger", tx_id)
            await self._safe(self.adapter.send_meter_sample(ev), "MeterValues")

    def _change_availability(self, cmd: d.ChangeAvailability) -> d.AvailabilityStatus:
        targets = list(self.conns.values()) if cmd.evse_id == 0 else [self.conns[cmd.evse_id]]
        scheduled = False
        changed: List[_Conn] = []
        for c in targets:
            if c.tx is not None and c.operative != cmd.operative:
                # TODO(spec): Scheduled is applied right after the transaction ends (StopTransaction queued).
                c.pending_operative = cmd.operative
                scheduled = True
            else:
                c.operative, c.pending_operative = cmd.operative, None
                if not cmd.operative:
                    c.pending_tag, c.pending_profile = None, None
                changed.append(c)
        station = cmd.evse_id == 0
        if station:
            if scheduled:
                self._station_pending = cmd.operative
            else:
                self._station_operative, self._station_pending = cmd.operative, None
        self._after_reply(functools.partial(self._refresh_after_availability, station and not scheduled,
                                            changed))
        return d.AvailabilityStatus.SCHEDULED if scheduled else d.AvailabilityStatus.ACCEPTED

    def _refresh_after_availability(self, station: bool, changed: List[_Conn]) -> None:
        if station:
            self._refresh_station()
        for c in changed:
            self._refresh(c)

    # ================================================================== public API (Web API, M6)

    def _conn(self, connector: int) -> _Conn:
        try:
            return self.conns[connector]
        except KeyError:
            raise ValueError(f"no connector {connector} (1..{self.connector_count})") from None

    async def plug(self, connector: int) -> None:
        c = self._conn(connector)
        if c.plugged:
            return
        c.plugged = True
        if c.pending_tag and c.operative and not c.fault:
            await self._start_tx(c, c.pending_tag, c.pending_profile)
        else:
            self._refresh(c)

    async def unplug(self, connector: int) -> None:
        """Never waits for the CSMS (StopTransaction / StatusNotification are queued)."""
        c = self._conn(connector)
        if not c.plugged:
            return
        c.plugged, c.finishing = False, False
        if c.tx is not None:
            # TODO(spec): StopTransactionOnEVSideDisconnect=false (SuspendedEV) is not simulated; always stop.
            self._end_tx(c, R.EV_DISCONNECTED)
        else:
            self._refresh(c)

    async def swipe(self, connector: int, id_tag: str) -> A:
        """Present an id tag. Returns the authorization status when it was refused, ``Accepted`` when a
        transaction started (or the tag waits for the cable), ``Stopped`` when it ended its own transaction,
        ``Pending`` when the connection was lost before the StartTransaction answer (the transaction is kept and
        StartTransaction resent after the next accepted boot), ``Busy`` / ``Unavailable`` / ``Offline`` /
        ``Error`` when no transaction could be started.

        ``Accepted`` means the CSMS accepted the start, even when the transaction was ended locally (unplug, stop,
        fault, Reset) while the StartTransaction answer was outstanding; its StopTransaction then follows."""
        c = self._conn(connector)
        tx = c.tx
        if tx is not None:
            if tx.id_tag == id_tag:
                self._end_tx(c, R.LOCAL, tx=tx)
                return A.STOPPED
            return A.BUSY  # TODO(spec): stopping with a different tag of the same parent id is not simulated
        if c.fault or not c.operative:
            return A.UNAVAILABLE
        status = await self._authorize(id_tag)
        if status is not A.ACCEPTED:
            return status
        return await self._start_tx(c, id_tag)

    async def stop_tx(self, connector: int) -> bool:
        """Never waits for the CSMS: the StopTransaction is queued before this returns."""
        c = self._conn(connector)
        if c.tx is None:
            return False
        self._end_tx(c, R.LOCAL)
        return True

    async def fault(self, connector: int, error_code: str) -> None:
        """``error_code`` is the version wire name (1.6 ``ChargePointErrorCode``, e.g. ``GroundFailure``)."""
        c = self._conn(connector)
        c.fault = error_code
        if c.tx is not None:
            self._end_tx(c, R.OTHER)  # TODO(spec): a fault ends the transaction with reason Other
        self._refresh(c)

    async def clear_fault(self, connector: int) -> None:
        c = self._conn(connector)
        c.fault = None
        self._refresh(c)

    async def send_data_transfer(self, vendor_id: str, message_id: Optional[str] = None,
                                 data: Optional[str] = None) -> d.DataTransferResult:
        if not self.connected:
            raise ConnectionError("charger is not connected")
        return await self.adapter.send_data_transfer(d.DataTransfer(vendor_id, message_id, data))

    def snapshot(self) -> Dict[str, Any]:
        def conn(c: _Conn) -> Dict[str, Any]:
            tx = None
            if c.tx is not None:
                tx = {"tx_id": c.tx.tx_id, "id_tag": c.tx.id_tag, "meter_start_wh": c.tx.meter_start_wh,
                      "started_at": c.tx.started_at.isoformat()}
            return {"id": c.id, "status": c.state.status.value, "error_code": c.state.error_code,
                    "plugged": c.plugged, "operative": c.operative, "pending_operative": c.pending_operative,
                    "pending_tag": c.pending_tag, "meter_wh": round(c.meter_wh, 3),
                    "power_w": round(c.pw.power_w, POWER_DECIMALS) if c.pw is not None else 0.0,
                    "soc_pct": round(c.pw.soc_pct, SOC_DECIMALS) if c.pw is not None else None,
                    "transaction": tx}

        return {
            "id": self.cfg.id,
            "ocpp_version": self.adapter.version,
            "connected": self.connected,
            "registration": self.registration,
            "last_error": dict(self.last_error) if self.last_error else None,
            "connect_attempts": self.connect_attempts,
            "boots": self.boots,
            "clock_offset_s": self.clock_offset_s,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "tx_queue": sum(1 for m in self._outq if m.transactional),
            "station": {"status": self.station.status.value, "operative": self._station_operative},
            "connectors": [conn(c) for c in self.conns.values()],
        }
