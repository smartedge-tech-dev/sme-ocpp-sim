"""Single charger runtime (plan §2.2, M3).

One :class:`ChargerRuntime` per charger owns the reconnect loop, the RPC connection, the version adapter, the
ConfigStore and the connector state machines. It speaks only the domain types of ``protocol.base`` and the
:class:`VersionAdapter` interface; it never sees version messages.

Task model: everything that needs the connection (boot / heartbeat loop, meter sampling, follow-ups of CSMS
commands) runs as a *session task* and is cancelled when the connection ends. Transactions survive a
disconnect; their meter loops restart after the next accepted boot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple

from ..config import ChargerConfig, ReconnectConfig
from ..protocol import base as d
from ..protocol.base import DomainStatus as S
from ..protocol.registry import get_adapter_class
from ..rpc import Backoff, HandshakeError, RpcConnection, TransportConfig, open_connection, wait_or_stop
from .config_store import ConfigStore
from .state import ConnectorState, IllegalTransition

log = logging.getLogger(__name__)

ENERGY = "Energy.Active.Import.Register"
POWER = "Power.Active.Import"
CURRENT = "Current.Import"
VOLTAGE = "Voltage"
NOMINAL_V = 230.0
LOCATION = "Outlet"

AUTH_ACCEPTED = "Accepted"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Tx:
    id_tag: str
    meter_start_wh: int
    started_at: datetime
    tx_id: Optional[str] = None  # set once the CSMS answered StartTransaction


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
            derived["MeterValueSampleInterval"] = str(cfg.meter_sample_interval_s)
        overrides = {k: v for k, v in derived.items() if k in names}
        overrides.update(cfg.config_keys)
        self.store = ConfigStore(self.adapter.config_keys, overrides)
        self.transport = TransportConfig(
            csms_url, cfg.id, subprotocol=self.adapter.subprotocol, password=cfg.password, ca_file=cfg.ca_file,
            verify_tls=cfg.verify_tls, require_subprotocol=cfg.require_subprotocol)
        rc = reconnect or ReconnectConfig()
        self._backoff = backoff or Backoff(rc.min_s, 2.0, rc.max_s, rc.jitter)
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

    # ================================================================== connection

    @property
    def connector_count(self) -> int:
        return len(self.conns)

    @property
    def online(self) -> bool:
        return self.connected and self.registration == d.BootStatus.ACCEPTED.value

    async def run(self, stop: asyncio.Event) -> None:
        """Connect, boot, serve; reconnect with back-off until ``stop`` is set."""
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
            self._backoff.reset()
            self.last_error = None
            was_reset = await self._session(ws, stop)
            if stop.is_set():
                break
            if not was_reset:
                self.last_error = {"kind": "disconnected", "message": "connection closed"}
                if await wait_or_stop(stop, self._backoff.next()):
                    break

    async def _session(self, ws: Any, stop: asyncio.Event) -> bool:
        """Serve one connection; returns True when it ended because of a Reset."""
        self._reset_done.clear()
        self.registration = None
        rpc = RpcConnection(ws, self.adapter.handlers(self), error_codes=self.adapter.error_codes,
                            call_timeout=self._call_timeout, on_frame=self._frame_cb, name=self.cfg.id)
        self.adapter.bind(rpc.call)
        self.connected = True
        rx = asyncio.create_task(rpc.run())
        waits = [rx, asyncio.create_task(stop.wait()), asyncio.create_task(self._reset_done.wait())]
        self._spawn(self._online())
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
                await asyncio.wait_for(rx, 5)
            except Exception:  # noqa: BLE001 - socket already broken
                pass
        return self._reset_done.is_set()

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

    def _after_reply(self, coro: Awaitable[Any]) -> None:
        """Run ``coro`` after the CALLRESULT of the current CSMS CALL went out."""
        async def later() -> None:
            # RpcConnection writes the CALLRESULT right after the handler returns; this short pause keeps
            # follow-up CALLs (StatusNotification, StopTransaction, ...) behind it on the wire.
            await asyncio.sleep(0.01)
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

    # ================================================================== boot / heartbeat

    async def _online(self) -> None:
        await self._boot()
        await self._send_status(self.station)
        for c in self.conns.values():
            await self._send_status(c.state)
        for c in self.conns.values():
            if c.tx is not None and c.tx.tx_id is not None:
                self._start_meter(c)
        while True:
            interval = self.store.get_int("HeartbeatInterval")
            await asyncio.sleep(interval if interval > 0 else 1)
            if interval > 0:
                await self._heartbeat()

    async def _boot(self) -> None:
        """BootNotification until Accepted; CSMS CALLs are served meanwhile by the RPC layer."""
        while True:
            self._boot_kick.clear()
            delay: float = self.cfg.boot_retry_s
            res = await self._send_boot()
            if res is not None:
                if res.status is d.BootStatus.ACCEPTED:
                    return
                if res.interval > 0:
                    delay = res.interval
            try:
                await asyncio.wait_for(self._boot_kick.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def _send_boot(self) -> Optional[d.BootResult]:
        ev = d.Booting(self.cfg.vendor, self.cfg.model, self.cfg.serial, self.cfg.firmware)
        res: Optional[d.BootResult] = await self._safe(self.adapter.send_booting(ev), "BootNotification")
        if res is None:
            return None
        self.boots += 1
        self.registration = res.status.value
        self._note_time(res.current_time)
        if res.status is d.BootStatus.ACCEPTED and res.interval > 0:
            self.store.set("HeartbeatInterval", str(res.interval))
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

    # ================================================================== status

    def _target(self, c: _Conn) -> S:
        if c.fault:
            return S.FAULTED
        if not c.operative:
            return S.UNAVAILABLE
        if c.tx is not None:
            return S.CHARGING
        if c.plugged and c.finishing:
            return S.FINISHING
        if c.plugged or c.pending_tag:
            return S.PREPARING
        return S.AVAILABLE

    def _apply(self, st: ConnectorState, new: S, error_code: Optional[str] = None) -> bool:
        try:
            return st.transition(new, error_code=error_code)
        except IllegalTransition as e:
            # TODO(spec): a transition outside the (unverified) 1.6 table is forced instead of refused, so the
            # simulator never gets stuck; it is logged for review.
            log.warning("%s: %s (forced)", self.cfg.id, e)
            st.status = new
            st.error_code = error_code if new is S.FAULTED else None
            return True

    async def _refresh(self, c: _Conn) -> None:
        if self._apply(c.state, self._target(c), c.fault) and self.online:
            await self._send_status(c.state)

    async def _refresh_station(self) -> None:
        new = S.AVAILABLE if self._station_operative else S.UNAVAILABLE
        if self._apply(self.station, new) and self.online:
            await self._send_status(self.station)

    async def _send_status(self, st: ConnectorState) -> None:
        ev = d.ConnectorStatus(st.connector_id, st.connector_id, st.status, st.error_code, st.info,
                               st.vendor_error_code, self._clock())
        await self._safe(self.adapter.send_connector_status(ev), "StatusNotification")

    # ================================================================== authorization / transactions

    async def _authorize(self, tag: str) -> str:
        mode = self.cfg.auth_mode
        if mode == "accept_all":
            return AUTH_ACCEPTED
        local = tag in self.cfg.id_tags
        if mode == "local_only":
            return AUTH_ACCEPTED if local else "Invalid"
        if mode == "local_then_csms" and local:
            return AUTH_ACCEPTED
        if not self.online:
            return "Offline"  # TODO(spec): offline authorization (LocalAuthorizeOffline / cache) is not simulated
        info: Optional[d.IdTokenInfo] = await self._safe(self.adapter.send_authorize(d.AuthorizeRequest(tag)),
                                                         "Authorize")
        return info.status if info is not None else "Error"

    def _accumulate(self, c: _Conn) -> None:
        now = self._mono()
        if c.tx is not None and c.last_mono is not None:
            c.meter_wh += self.cfg.power_w * (now - c.last_mono) / 3600.0
        c.last_mono = now

    def _samples(self, c: _Conn, measurands: Iterable[str]) -> List[d.Sample]:
        power = self.cfg.power_w if c.tx is not None else 0.0
        out: List[d.Sample] = []
        for name in measurands:
            if name == ENERGY:
                out.append(d.Sample(name, round(c.meter_wh), "Wh", location=LOCATION))
            elif name == POWER:
                out.append(d.Sample(name, power, "W", location=LOCATION))
            elif name == CURRENT:
                out.append(d.Sample(name, round(power / NOMINAL_V, 3), "A", location=LOCATION))
            elif name == VOLTAGE:
                out.append(d.Sample(name, NOMINAL_V, "V", location=LOCATION))
            # M4: the power / EV model provides the remaining measurands (SoC, Current.Offered, ...)
        return out

    async def _start_tx(self, c: _Conn, tag: str) -> bool:
        async with c.lock:
            if c.tx is not None:
                return False
            c.pending_tag = None
            c.finishing = False
            await self._refresh(c)  # Preparing
            if not self.online:
                return False  # TODO(spec): offline transactions are not simulated in v0.1
            meter_start, ts = int(round(c.meter_wh)), self._clock()
            c.tx = _Tx(tag, meter_start, ts)
            try:
                res = await self.adapter.send_tx_started(d.TxStarted(c.id, tag, meter_start, ts))
            except asyncio.CancelledError:
                c.tx = None
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("%s: StartTransaction failed: %r", self.cfg.id, e)
                c.tx = None
                await self._refresh(c)
                return False
            c.tx.tx_id = res.tx_id
            c.last_mono = self._mono()
            if not res.id_token_info.accepted and self.store.get_bool("StopTransactionOnInvalidId"):
                # TODO(spec): MaxEnergyOnInvalidId / suspending instead of stopping is not simulated.
                await self._end_tx_locked(c, "DeAuthorized")
                return False
            await self._refresh(c)  # Charging
            self._start_meter(c)
            return True

    def _start_meter(self, c: _Conn) -> None:
        if c.meter_task is None or c.meter_task.done():
            c.meter_task = self._spawn(self._meter_loop(c))

    async def _meter_loop(self, c: _Conn) -> None:
        # TODO(spec): clock-aligned readings (ClockAlignedDataInterval / MeterValuesAlignedData) are not sent yet.
        while c.tx is not None:
            interval = self.store.get_int("MeterValueSampleInterval")
            await asyncio.sleep(interval if interval > 0 else 1)
            tx = c.tx
            if interval <= 0 or tx is None or tx.tx_id is None:
                continue
            self._accumulate(c)
            samples = self._samples(c, self.store.get_csl("MeterValuesSampledData"))
            await self._safe(self.adapter.send_tx_updated(d.TxUpdated(c.id, tx.tx_id, self._clock(), samples)),
                             "MeterValues")

    async def _end_tx(self, c: _Conn, reason: str) -> None:
        async with c.lock:
            await self._end_tx_locked(c, reason)

    async def _end_tx_locked(self, c: _Conn, reason: str) -> None:
        tx = c.tx
        if tx is None:
            return
        if c.meter_task is not None and c.meter_task is not asyncio.current_task():
            c.meter_task.cancel()
        c.meter_task = None
        self._accumulate(c)
        ts, stop_wh = self._clock(), int(round(c.meter_wh))
        samples = self._samples(c, self.store.get_csl("StopTxnSampledData"))
        c.tx, c.last_mono, c.finishing = None, None, c.plugged
        if c.pending_operative is not None:
            c.operative, c.pending_operative = c.pending_operative, None
        if tx.tx_id is not None:
            # TODO(spec): StopTransaction is sent before the Finishing / Available StatusNotification.
            await self._safe(self.adapter.send_tx_ended(
                d.TxEnded(c.id, tx.tx_id, stop_wh, ts, reason, tx.id_tag, samples)), "StopTransaction")
        await self._refresh(c)
        if self._station_pending is not None and all(x.tx is None for x in self.conns.values()):
            self._station_operative, self._station_pending = self._station_pending, None
            await self._refresh_station()

    # ================================================================== CSMS commands

    async def handle(self, cmd: d.Command) -> Any:
        if isinstance(cmd, d.RequestStart):
            return self._remote_start(cmd)
        if isinstance(cmd, d.RequestStop):
            return self._remote_stop(cmd)
        if isinstance(cmd, d.GetConfig):
            known, unknown = self.store.get(cmd.keys)
            return d.GetConfigResult(known, unknown)
        if isinstance(cmd, d.SetConfig):
            return self.store.set(cmd.key, cmd.value)
        if isinstance(cmd, d.Reset):
            self._after_reply(self._do_reset(cmd.kind))
            return d.CommandStatus.ACCEPTED
        if isinstance(cmd, d.Trigger):
            return self._trigger(cmd)
        if isinstance(cmd, d.Unlock):
            c = self.conns[cmd.evse_id]
            if c.tx is not None:
                self._after_reply(self._end_tx(c, "UnlockCommand"))
            return d.UnlockStatus.UNLOCKED  # TODO(spec): UnlockFailed / NotSupported answers are not configurable yet
        if isinstance(cmd, d.ChangeAvailability):
            return self._change_availability(cmd)
        if isinstance(cmd, d.DataTransfer):
            rule = (self.data_transfer_rules.get((cmd.vendor_id, cmd.message_id))
                    or self.data_transfer_rules.get((cmd.vendor_id, None)))
            return rule or d.DataTransferResult(d.DataTransferStatus.UNKNOWN_VENDOR_ID)
        return d.CommandStatus.NOT_IMPLEMENTED  # M4: SetProfile / ClearProfiles / GetComposite

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
        if self.store.get_bool("AuthorizeRemoteTxRequests") and await self._authorize(tag) != AUTH_ACCEPTED:
            c.pending_tag = None
            await self._refresh(c)
            return
        if c.plugged:
            await self._start_tx(c, tag)
        else:
            # TODO(spec): ConnectionTimeOut (cable not plugged in time) is not enforced.
            await self._refresh(c)

    def _remote_stop(self, cmd: d.RequestStop) -> d.CommandStatus:
        for c in self.conns.values():
            if c.tx is not None and c.tx.tx_id == cmd.tx_id:
                self._after_reply(self._end_tx(c, "Remote"))
                return d.CommandStatus.ACCEPTED
        return d.CommandStatus.REJECTED

    async def _do_reset(self, kind: d.ResetKind) -> None:
        # TODO(spec): both Soft and Hard reset stop running transactions before closing (reason SoftReset /
        # HardReset); a real Hard reset may send StopTransaction only after the reboot.
        reason = "SoftReset" if kind is d.ResetKind.SOFT else "HardReset"
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
                self._after_reply(self._send_boot())
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
            await self._end_tx(c, "EVDisconnected")
        else:
            await self._refresh(c)

    async def swipe(self, connector: int, id_tag: str) -> str:
        """Present an id tag. Returns the authorization status, ``"Stopped"`` when it ended its own transaction,
        ``"Busy"`` / ``"Unavailable"`` when the connector cannot start one."""
        c = self._conn(connector)
        if c.tx is not None:
            if c.tx.id_tag == id_tag:
                await self._end_tx(c, "Local")
                return "Stopped"
            return "Busy"  # TODO(spec): stopping with a different tag of the same parent id is not simulated
        if c.fault or not c.operative:
            return "Unavailable"
        status = await self._authorize(id_tag)
        if status != AUTH_ACCEPTED:
            return status
        if c.plugged:
            await self._start_tx(c, id_tag)
        else:
            c.pending_tag = id_tag
            await self._refresh(c)
        return status

    async def stop_tx(self, connector: int) -> bool:
        c = self._conn(connector)
        if c.tx is None:
            return False
        await self._end_tx(c, "Local")
        return True

    async def fault(self, connector: int, error_code: str) -> None:
        """``error_code`` is the version wire name (1.6 ``ChargePointErrorCode``, e.g. ``GroundFailure``)."""
        c = self._conn(connector)
        c.fault = error_code
        if c.tx is not None:
            await self._end_tx(c, "Other")  # TODO(spec): a fault ends the transaction with reason Other
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
            "station": {"status": self.station.status.value, "operative": self._station_operative},
            "connectors": [conn(c) for c in self.conns.values()],
        }
