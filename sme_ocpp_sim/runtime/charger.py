"""Single charger runtime (plan §2.2, M3).

One :class:`ChargerRuntime` per charger owns the reconnect loop, the RPC connection, the version adapter, the
ConfigStore and the connector state machines. It speaks only the domain types of ``protocol.base`` and the
:class:`VersionAdapter` interface; it never sees version messages.

Task model: everything that needs the connection (boot / heartbeat loop, meter sampling, transaction message
flushing, follow-ups of CSMS commands) runs as a *session task* and is cancelled when the connection ends.
Transactions survive a disconnect; their meter loops restart after the next accepted boot.

Transaction-related messages (core spec §3.7 p.17-18): StopTransaction and periodic MeterValues go through one
ordered queue. When offline (or while older messages are still queued) they are queued and flushed in order after
the next accepted boot. A CSMS "failure to process" (CALLERROR, timeout, unparsable answer) is retried
``TransactionMessageAttempts`` times, waiting ``TransactionMessageRetryInterval`` x preceding transmissions
before each retry (§3.7.1 p.17-18, §9.1.31 / §9.1.32 p.105); a lost connection is not a failure, the message
just stays queued.

Locking: ``_Conn.lock`` only guards state changes; CSMS calls are made after releasing it, so ``unplug`` /
``stop_tx`` / Reset never wait for a slow CSMS answer. ``_txq_lock`` serializes transaction messages on the wire.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Coroutine, Deque, Dict, Iterable, List, Optional, Set, Tuple

from ..config import ChargerConfig, ReconnectConfig
from ..protocol import base as d
from ..protocol.base import AuthResult as A
from ..protocol.base import DomainStatus as S
from ..protocol.base import StopReason as R
from ..protocol.registry import get_adapter_class
from ..rpc import Backoff, HandshakeError, RpcConnection, TransportConfig, open_connection, wait_event, wait_or_stop
from .config_store import ConfigStore
from .state import ConnectorState, IllegalTransition

log = logging.getLogger(__name__)

# Measurand names as used in the MeterValues*SampledData / StopTxn*Data configuration values.
ENERGY = "Energy.Active.Import.Register"
POWER = "Power.Active.Import"
CURRENT = "Current.Import"
VOLTAGE = "Voltage"
#: Single-phase placeholder until the M4 power / EV model (phases, real voltage).
NOMINAL_V = 230.0
CURRENT_DECIMALS = 3
LOCATION = "Outlet"

BACKOFF_FACTOR = 2.0
#: RpcConnection writes the CALLRESULT right after the handler returns; follow-up CALLs (StatusNotification,
#: StopTransaction, ...) wait this long so they go out behind it on the wire.
REPLY_DELAY_S = 0.01
#: How long a closed session waits for the receive loop to finish.
RX_DRAIN_TIMEOUT_S = 5.0

HEARTBEAT_KEY = "HeartbeatInterval"
SAMPLE_KEY = "MeterValueSampleInterval"
_INTERVAL_KEYS = (HEARTBEAT_KEY, SAMPLE_KEY)


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
    tx: Optional[_Tx] = None
    meter_wh: float = 0.0                     # energy register
    last_mono: Optional[float] = None
    meter_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _Delivery(Enum):
    DONE = "done"        # delivered, or discarded after TransactionMessageAttempts failures
    RETRY = "retry"      # failure to process; retry after the retry interval
    OFFLINE = "offline"  # connection lost; keep queued for the next session


@dataclass
class _TxMsg:
    what: str
    send: Callable[[], Awaitable[Any]]
    keep_on_cancel: bool = False  # re-queue when the sending task is cancelled mid-call
    failures: int = 0


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
    ) -> None:
        self.cfg = cfg
        self.adapter: d.VersionAdapter = get_adapter_class(ocpp_version)(strict=strict)
        names = {k.name for k in self.adapter.config_keys}
        derived = {"NumberOfConnectors": str(cfg.connectors)}
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

        self._txq: Deque[_TxMsg] = deque()
        self._txq_lock = asyncio.Lock()
        self._txq_kick = asyncio.Event()
        self._txq_idle = asyncio.Event()
        self._txq_idle.set()

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
        waits = [rx, asyncio.create_task(stop.wait()), asyncio.create_task(self._reset_done.wait()),
                 asyncio.create_task(self._abort.wait())]
        self._spawn(self._online_guarded())
        rx_error: Optional[str] = None
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.connected = False
            self.adapter.bind(None)
            for w in waits[1:]:
                w.cancel()
            await self._cancel_tasks()
            await rpc.close()
            try:
                async with asyncio.timeout(RX_DRAIN_TIMEOUT_S):
                    await asyncio.shield(rx)
            except TimeoutError:
                rx.cancel()
                rx_error = "receive loop did not finish"
                log.warning("%s: %s", self.cfg.id, rx_error)
            except Exception as e:  # noqa: BLE001 - record why the connection died
                rx_error = repr(e)
                log.warning("%s: receive loop failed: %r", self.cfg.id, e)
        if not self._reset_done.is_set() and not stop.is_set() and not self._abort.is_set():
            self.last_error = {"kind": "disconnected", "message": self._close_text(ws, rx_error)}
        return self._reset_done.is_set()

    @staticmethod
    def _close_text(ws: Any, rx_error: Optional[str]) -> str:
        code, reason = getattr(ws, "close_code", None), getattr(ws, "close_reason", None)
        text = "connection closed"
        if code is not None:
            text += f" (code {code}{': ' + reason if reason else ''})"
        return f"{text}; {rx_error}" if rx_error else text

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
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

    def _after_reply(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run ``coro`` after the CALLRESULT of the current CSMS CALL went out."""
        async def later() -> None:
            try:
                await asyncio.sleep(REPLY_DELAY_S)
            except asyncio.CancelledError:
                coro.close()  # never started: close it so it is not reported as "never awaited"
                raise
            await coro
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
        self._spawn(self._flush_tx())
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

    # ================================================================== transaction-related messages

    async def _tx_send(self, what: str, send: Callable[[], Awaitable[Any]], *, keep_on_cancel: bool = False) -> None:
        """Deliver a transaction-related message now if possible, otherwise queue it (core spec §3.7 p.17)."""
        msg = _TxMsg(what, send, keep_on_cancel)
        async with self._txq_lock:
            if self.online and not self._txq:
                try:
                    if await self._deliver(msg) is _Delivery.DONE:
                        return
                except asyncio.CancelledError:
                    if keep_on_cancel:
                        self._txq_push(msg)
                    raise
            self._txq_push(msg)

    def _txq_push(self, msg: _TxMsg) -> None:
        self._txq.append(msg)
        self._txq_idle.clear()
        self._txq_kick.set()

    async def _deliver(self, msg: _TxMsg) -> _Delivery:
        try:
            await msg.send()
            return _Delivery.DONE
        except asyncio.CancelledError:
            raise
        except ConnectionError as e:
            log.info("%s: %s queued (%r)", self.cfg.id, msg.what, e)
            return _Delivery.OFFLINE
        except Exception as e:  # noqa: BLE001 - CALLERROR / timeout / bad answer = failure to process
            msg.failures += 1
            attempts = max(1, self.store.get_int("TransactionMessageAttempts"))
            if msg.failures >= attempts:
                log.warning("%s: %s discarded after %d failed attempts: %r", self.cfg.id, msg.what,
                            msg.failures, e)
                return _Delivery.DONE
            log.warning("%s: %s failed (%d/%d): %r", self.cfg.id, msg.what, msg.failures, attempts, e)
            return _Delivery.RETRY

    async def _flush_tx(self) -> None:
        """Session task: send queued transaction messages in order while online."""
        while True:
            await self._txq_kick.wait()
            self._txq_kick.clear()
            while self._txq and self.online:
                msg = self._txq[0]
                if msg.failures:
                    await asyncio.sleep(self.store.get_int("TransactionMessageRetryInterval") * msg.failures)
                async with self._txq_lock:
                    res = await self._deliver(msg)
                    if res is _Delivery.OFFLINE:
                        return  # the session is ending; the next accepted boot flushes again
                    if res is _Delivery.DONE:
                        self._txq.popleft()
                        if not self._txq:
                            self._txq_idle.set()

    async def _txq_drained(self) -> bool:
        """New transaction messages wait for the queue (§3.7 p.17); bounded by the call timeout."""
        return await wait_event(self._txq_idle, self._call_timeout)

    # ================================================================== status

    def _target(self, c: _Conn) -> S:
        if c.fault:
            return S.FAULTED
        if not c.operative:
            return S.UNAVAILABLE
        if c.tx is not None:
            # Charging only once the CSMS answered StartTransaction.
            return S.CHARGING if c.tx.tx_id is not None else S.PREPARING
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

    async def _refresh(self, c: _Conn) -> None:
        if self._apply(c.state, self._target(c), c.fault) and self.online:
            await self._send_status(c.state)

    async def _refresh_station(self) -> None:
        if self._apply(self.station, self._station_target()) and self.online:
            await self._send_status(self.station)

    async def _send_status(self, st: ConnectorState) -> None:
        ev = d.ConnectorStatus(st.connector_id, st.connector_id, st.status, st.error_code, st.info,
                               st.vendor_error_code, self._clock())
        await self._safe(self.adapter.send_connector_status(ev), "StatusNotification")

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

    def _accumulate(self, c: _Conn) -> None:
        now = self._mono()
        if c.tx is not None and c.last_mono is not None:
            c.meter_wh += self.cfg.power_w * (now - c.last_mono) / 3600.0
        c.last_mono = now

    def _samples(self, c: _Conn, measurands: Iterable[str]) -> List[d.Sample]:
        power = self.cfg.power_w if c.tx is not None and c.tx.tx_id is not None else 0.0
        out: List[d.Sample] = []
        for name in measurands:
            if name == ENERGY:
                out.append(d.Sample(name, round(c.meter_wh), "Wh", location=LOCATION))
            elif name == POWER:
                out.append(d.Sample(name, power, "W", location=LOCATION))
            elif name == CURRENT:
                out.append(d.Sample(name, round(power / NOMINAL_V, CURRENT_DECIMALS), "A", location=LOCATION))
            elif name == VOLTAGE:
                out.append(d.Sample(name, NOMINAL_V, "V", location=LOCATION))
            # M4: the power / EV model provides the remaining measurands (SoC, Current.Offered, ...)
        return out

    async def _start_tx(self, c: _Conn, tag: str) -> A:
        """Start a transaction for an authorized ``tag``; the state is re-checked here (an await may lie between
        authorization and this call). Not plugged = keep the tag and wait for the cable (Accepted)."""
        async with c.lock:
            if c.tx is not None:
                return A.BUSY
            if c.fault or not c.operative:
                return A.UNAVAILABLE
            c.finishing = False
            if not c.plugged:
                c.pending_tag = tag
                tx = None
                outcome = A.ACCEPTED
            elif not self.online:
                # TODO(spec): offline transactions are not simulated in v0.1 (§3.7 p.17 would queue a
                # StartTransaction without transactionId); the start is refused with Offline.
                c.pending_tag = None
                tx = None
                outcome = A.OFFLINE
            else:
                c.pending_tag = None
                tx = c.tx = _Tx(tag, int(round(c.meter_wh)), self._clock())
                outcome = A.ACCEPTED
        await self._refresh(c)  # Preparing
        if tx is None:
            return outcome
        return await self._send_start(c, tx)

    async def _send_start(self, c: _Conn, tx: _Tx) -> A:
        # TODO(spec): StartTransaction is not queued (it needs the transactionId answer); it waits for the queue
        # to drain (§3.7 p.17) up to the call timeout and fails the start otherwise.
        ev = d.TxStarted(c.id, tx.id_tag, tx.meter_start_wh, tx.started_at)
        res: Optional[d.TxStartedResult] = None
        try:
            if await self._txq_drained():
                async with self._txq_lock:
                    res = await self.adapter.send_tx_started(ev)
            else:
                log.warning("%s: StartTransaction not sent, transaction message queue not drained", self.cfg.id)
        except asyncio.CancelledError:
            if c.tx is tx:
                c.tx = None
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("%s: StartTransaction failed: %r", self.cfg.id, e)
        if res is None:
            async with c.lock:
                if c.tx is tx:
                    c.tx = None
            await self._refresh(c)
            return A.ERROR
        tx.tx_id = res.tx_id
        async with c.lock:
            ended_meanwhile = c.tx is not tx
            if not ended_meanwhile:
                c.last_mono = self._mono()
        if ended_meanwhile:
            # unplug / stop / fault while StartTransaction was in flight: the CSMS now knows the transaction.
            await self._send_stop(c, tx)
            return A.ACCEPTED
        if not res.id_token_info.accepted and self.store.get_bool("StopTransactionOnInvalidId"):
            # TODO(spec): MaxEnergyOnInvalidId / suspending instead of stopping is not simulated.
            await self._end_tx(c, R.DE_AUTHORIZED, tx=tx)
            return res.id_token_info.status
        await self._refresh(c)  # Charging
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
            self._accumulate(c)
            samples = self._samples(c, self.store.get_csl("MeterValuesSampledData"))
            ev = d.TxUpdated(c.id, tx.tx_id, self._clock(), samples)
            await self._tx_send("MeterValues", lambda ev=ev: self.adapter.send_tx_updated(ev))

    async def _end_tx(self, c: _Conn, reason: R, *, tx: Optional[_Tx] = None) -> None:
        """End the running transaction (only if it is ``tx`` when given)."""
        async with c.lock:
            cur = c.tx
            if cur is None or (tx is not None and cur is not tx):
                return
            if c.meter_task is not None and c.meter_task is not asyncio.current_task():
                c.meter_task.cancel()
            c.meter_task = None
            self._accumulate(c)
            cur.ended = _Stop(int(round(c.meter_wh)), self._clock(), reason,
                              self._samples(c, self.store.get_csl("StopTxnSampledData")))
            c.tx, c.last_mono, c.finishing = None, None, c.plugged
            if c.pending_operative is not None:
                c.operative, c.pending_operative = c.pending_operative, None
            station = self._station_pending is not None and all(x.tx is None for x in self.conns.values())
            if station:
                self._station_operative, self._station_pending = self._station_pending, None
        if cur.tx_id is not None:
            # TODO(spec): StopTransaction is sent before the Finishing / Available StatusNotification.
            await self._send_stop(c, cur)
        # else: StartTransaction is still in flight; _send_start sends the StopTransaction once it has the id.
        await self._refresh(c)
        if station:
            await self._refresh_station()

    async def _send_stop(self, c: _Conn, tx: _Tx) -> None:
        st = tx.ended
        assert st is not None and tx.tx_id is not None
        ev = d.TxEnded(c.id, tx.tx_id, st.meter_stop_wh, st.timestamp, st.reason, tx.id_tag, st.samples)
        await self._tx_send("StopTransaction", lambda: self.adapter.send_tx_ended(ev), keep_on_cancel=True)

    # ================================================================== CSMS commands

    async def handle(self, cmd: d.Command) -> Any:
        fn = self._commands.get(type(cmd))
        return fn(cmd) if fn is not None else d.CommandStatus.NOT_IMPLEMENTED  # M4: SetProfile / ...

    def _get_config(self, cmd: d.GetConfig) -> d.GetConfigResult:
        known, unknown = self.store.get(cmd.keys)
        return d.GetConfigResult(known, unknown)

    def _reset(self, cmd: d.Reset) -> d.CommandStatus:
        self._after_reply(self._do_reset(cmd.kind))
        return d.CommandStatus.ACCEPTED

    def _unlock(self, cmd: d.Unlock) -> d.UnlockStatus:
        c = self.conns[cmd.evse_id]
        if c.tx is not None:
            self._after_reply(self._end_tx(c, R.UNLOCK_COMMAND, tx=c.tx))
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
        c.pending_tag = cmd.id_token  # reserve until the follow-up runs
        self._after_reply(self._remote_start_go(c, cmd.id_token))
        return d.CommandStatus.ACCEPTED

    async def _remote_start_go(self, c: _Conn, tag: str) -> None:
        if self.store.get_bool("AuthorizeRemoteTxRequests") and await self._authorize(tag) is not A.ACCEPTED:
            c.pending_tag = None
            await self._refresh(c)
            return
        # TODO(spec): ConnectionTimeOut (cable not plugged in time) is not enforced.
        await self._start_tx(c, tag)

    def _remote_stop(self, cmd: d.RequestStop) -> d.CommandStatus:
        for c in self.conns.values():
            if c.tx is not None and c.tx.tx_id == cmd.tx_id:
                self._after_reply(self._end_tx(c, R.REMOTE, tx=c.tx))
                return d.CommandStatus.ACCEPTED
        return d.CommandStatus.REJECTED

    async def _do_reset(self, kind: d.ResetKind) -> None:
        # TODO(spec): both Soft and Hard reset stop running transactions before closing (reason SoftReset /
        # HardReset); a real Hard reset may send StopTransaction only after the reboot.
        reason = R.SOFT_RESET if kind is d.ResetKind.SOFT else R.HARD_RESET
        for c in self.conns.values():
            await self._end_tx(c, reason)
            c.pending_tag = None
        self._reset_done.set()

    def _trigger(self, cmd: d.Trigger) -> d.CommandStatus:
        what = cmd.what
        if what is d.TriggerWhat.BOOT:
            if self.registration != d.BootStatus.ACCEPTED.value:
                self._boot_kick.set()
            else:
                self._after_reply(self._send_boot(triggered=True))
        elif what is d.TriggerWhat.HEARTBEAT:
            self._after_reply(self._heartbeat())
        elif what is d.TriggerWhat.STATUS:
            targets = ([self.station] if cmd.evse_id in (None, 0) else []) + [
                c.state for c in self.conns.values() if cmd.evse_id in (None, c.id)]
            self._after_reply(self._send_statuses(targets))
        elif what is d.TriggerWhat.METER_VALUES:
            # TODO(spec): connectorId 0 / absent triggers a reading per connector (no main meter is simulated).
            targets = [c for c in self.conns.values() if cmd.evse_id in (None, 0, c.id)]
            self._after_reply(self._send_readings(targets))
        else:
            return d.CommandStatus.NOT_IMPLEMENTED
        return d.CommandStatus.ACCEPTED

    async def _send_statuses(self, states: List[ConnectorState]) -> None:
        for st in states:
            await self._send_status(st)

    async def _send_readings(self, conns: List[_Conn]) -> None:
        for c in conns:
            self._accumulate(c)
            tx_id = c.tx.tx_id if c.tx is not None else None
            ev = d.MeterSample(c.id, self._clock(), self._samples(c, self.store.get_csl("MeterValuesSampledData")),
                               "Trigger", tx_id)
            await self._safe(self.adapter.send_meter_sample(ev), "MeterValues")

    def _change_availability(self, cmd: d.ChangeAvailability) -> d.AvailabilityStatus:
        targets = list(self.conns.values()) if cmd.evse_id == 0 else [self.conns[cmd.evse_id]]
        scheduled = False
        changed: List[_Conn] = []
        for c in targets:
            if c.tx is not None and c.operative != cmd.operative:
                # TODO(spec): Scheduled is applied right after the transaction ends (StopTransaction sent).
                c.pending_operative = cmd.operative
                scheduled = True
            else:
                c.operative, c.pending_operative = cmd.operative, None
                if not cmd.operative:
                    c.pending_tag = None
                changed.append(c)
        station = cmd.evse_id == 0
        if station:
            if scheduled:
                self._station_pending = cmd.operative
            else:
                self._station_operative, self._station_pending = cmd.operative, None

        async def apply() -> None:
            if station and not scheduled:
                await self._refresh_station()
            for c in changed:
                await self._refresh(c)

        self._after_reply(apply())
        return d.AvailabilityStatus.SCHEDULED if scheduled else d.AvailabilityStatus.ACCEPTED

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
            await self._start_tx(c, c.pending_tag)
        else:
            await self._refresh(c)

    async def unplug(self, connector: int) -> None:
        c = self._conn(connector)
        if not c.plugged:
            return
        c.plugged, c.finishing = False, False
        if c.tx is not None:
            # TODO(spec): StopTransactionOnEVSideDisconnect=false (SuspendedEV) is not simulated; always stop.
            await self._end_tx(c, R.EV_DISCONNECTED)
        else:
            await self._refresh(c)

    async def swipe(self, connector: int, id_tag: str) -> A:
        """Present an id tag. Returns the authorization status when it was refused, ``Accepted`` when a
        transaction started (or the tag waits for the cable), ``Stopped`` when it ended its own transaction,
        ``Busy`` / ``Unavailable`` / ``Offline`` / ``Error`` when no transaction could be started."""
        c = self._conn(connector)
        tx = c.tx
        if tx is not None:
            if tx.id_tag == id_tag:
                await self._end_tx(c, R.LOCAL, tx=tx)
                return A.STOPPED
            return A.BUSY  # TODO(spec): stopping with a different tag of the same parent id is not simulated
        if c.fault or not c.operative:
            return A.UNAVAILABLE
        status = await self._authorize(id_tag)
        if status is not A.ACCEPTED:
            return status
        return await self._start_tx(c, id_tag)

    async def stop_tx(self, connector: int) -> bool:
        c = self._conn(connector)
        if c.tx is None:
            return False
        await self._end_tx(c, R.LOCAL)
        return True

    async def fault(self, connector: int, error_code: str) -> None:
        """``error_code`` is the version wire name (1.6 ``ChargePointErrorCode``, e.g. ``GroundFailure``)."""
        c = self._conn(connector)
        c.fault = error_code
        if c.tx is not None:
            await self._end_tx(c, R.OTHER)  # TODO(spec): a fault ends the transaction with reason Other
        await self._refresh(c)

    async def clear_fault(self, connector: int) -> None:
        c = self._conn(connector)
        c.fault = None
        await self._refresh(c)

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
                    "pending_tag": c.pending_tag, "meter_wh": round(c.meter_wh, 3), "transaction": tx}

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
            "tx_queue": len(self._txq),
            "station": {"status": self.station.status.value, "operative": self._station_operative},
            "connectors": [conn(c) for c in self.conns.values()],
        }
