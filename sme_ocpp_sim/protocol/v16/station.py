"""OCPP 1.6 station: connector state machine, transactions, configuration keys, smart charging (plan §11.2).

One :class:`Station16` per 1.6 charger, hosted by the shared shell ``runtime.charger.ChargerRuntime`` through
the :class:`~sme_ocpp_sim.protocol.station.StationHost` interface. It speaks the domain types of
``protocol.base`` and the 1.6 :class:`Adapter16`; it never sees 1.6 messages. Session tasks are started with
``host.spawn``, so they end with the session. Transactions survive a disconnect; their meter loops restart
after the next accepted boot.

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

Power model and smart charging (M4b, design §3.2 / §3.3): the station owns a :class:`ProfileStore`. While a
transaction is charging (StartTransaction answered), the connector's power model is stepped every ``tick_s`` and
before every meter reading / StopTransaction: ``composite.limit_at`` (the evaluator that also answers
GetCompositeSchedule, fed with the same per-phase voltage and phases) -> ``power.step`` -> power, energy register,
SoC. The suspension cause of the power model moves the connector Charging <-> SuspendedEV / SuspendedEVSE; the
StatusNotification goes through the outbox like any other state change. Every transaction starts a fresh EV at
``session.initial_soc``; when it ends the connector's TxProfiles are dropped (1.6 §3.13.1, §7.10). A
RemoteStartTransaction profile travels with the reservation / transaction and is installed, bound to the
transactionId, once StartTransaction is answered (§5.16.2); a start that is refused or fails discards it.

Timing: a SetChargingProfile / ClearChargingProfile first integrates every charging connector up to now at the old
limit; the new limit takes effect on the tick scheduled right after the CALLRESULT is sent (``host.after_reply``),
not before the CSMS has the answer. ``time_scale`` scales only the power model (ramp, energy register, SoC);
profile evaluation always uses the wall clock (``clock``), because schedules are in CSMS time and must match what
GetCompositeSchedule reports. A Relative 60 s period therefore lasts 60 wall-clock seconds at any time_scale.

Pause: while ``host.paused`` the power model is frozen (``_advance`` only restarts the interval); see
``runtime.charger`` for the pause semantics.

Interpretations (not defined by the spec): a limit in A applies to the phases the EV uses while one charges and to
the supply's phases otherwise (errata 3.34); GetCompositeSchedule periods are capped at the hardware maximum
(``max_power_w``, times the connector count for connectorId 0), which also stands for "no limit"; ticks are
session tasks, so after an offline gap the first step integrates the gap at the limit in force on reconnect.

Local authorization (M6, §3.5, §5.4, §5.10, §5.15, errata 3.52): the station keeps a Local Authorization List
and an Authorization Cache (``runtime.local_auth``, in memory, kept across reconnects and Resets). A presented
tag is authorized in this order: ``auth_mode`` ``accept_all`` -> Accepted; the entry of the Local Authorization
List (LocalAuthListEnabled) or else of the cache (AuthorizationCacheEnabled) is looked up (a list entry has
priority, §3.5.3; an Accepted list entry whose expiryDate has passed counts as Expired, §3.5.4); ``local_only``:
``id_tags`` -> Accepted, else the entry's status, else Invalid; ``local_then_csms`` and in ``id_tags`` ->
Accepted; offline: the entry when LocalAuthorizeOffline, else ``Offline``; online: an Accepted entry when
LocalPreAuthorize, else Authorize.req (also for an entry that is not Accepted, §3.5.1). Every IdTagInfo received
(Authorize, StartTransaction, StopTransaction) updates the cache unless the tag is in the Local Authorization
List (§3.5.1, §3.5.3).

Reservations (M6, §3.11, §5.1, §5.13): ReserveNow answers Faulted / Unavailable / Occupied for a faulted,
inoperative or occupied (transaction, cable plugged, tag waiting, reserved) connector, Rejected for connector 0
while ReserveConnectorZeroSupported is false, else Accepted; a reservation with the same id is replaced. A
reserved connector reports Reserved; it only starts for the reserved idTag or a tag whose parent is the
reservation's parentIdTag (§5.13 read pairwise, see ``runtime.local_auth``; the parent comes from the
authorization, the Local Authorization List or the cache, else from an Authorize.req). Any other tag is refused
(``Reserved``). A connector-0 reservation keeps one connector free: while the free connectors are no more than
the connector-0 reservations they report Reserved, and a non-matching tag is refused when its start would leave
fewer free connectors than connector-0 reservations. "Free" = no fault, operative, no transaction, no waiting tag,
not reserved; a plugged cable does not make a connector unavailable for the reservation (it authorizes nothing),
but a specific ReserveNow on a plugged connector is Occupied. A matching tag that waits (for the cable, or a
RemoteStart being authorized) claims the reservation: it stays stored but holds no connector; it ends when the
transaction starts (the StartTransaction carries its ``reservationId``) and holds its connector again when the
tag is dropped (failed authorization, Reset, availability change). A reservation ends when it is used, when
``expiryDate`` is reached (StatusNotification Available), when CancelReservation names it, or when its connector
(connector 0: the charge point) becomes Faulted or Unavailable. Stopping the charger cancels the expiry timers;
the next accepted boot re-arms them (an expired reservation then ends at once).

Interpretations (M6, not defined by the spec): SendLocalList while LocalAuthListEnabled is false is answered
NotSupported; one with more entries than SendLocalListMaxLength, duplicate tags, a Full entry without idTagInfo or
a result longer than LocalAuthListMaxLength is Failed (the list is unchanged); a Full update may set any version
> 0, a Differential one must raise it (VersionMismatch). GetLocalListVersion reports the stored version (0 = no
list received yet); while LocalAuthListEnabled is false it reports 0 (§5.10: 0 = the list is empty, -1 = Local
Authorization Lists not supported; the profile is supported, a disabled list is used as an empty one, as 2.0.1
D02.FR.03). ClearCache is Rejected while AuthorizationCacheEnabled is
false (as 2.0.1 C11.FR.04). ReserveNow with an expiryDate that is not in the future is Rejected. A replacing
ReserveNow is checked as if the old reservation did not exist; when it is refused, the old one stays. RemoteStartTransaction on a reserved connector compares the request's idTag with the reservation's
idTag / parentIdTag only (no parent lookup). The expiry timer runs on the wall clock (not scaled, not paused).
TODO(spec): the LocalListConflict StatusNotification (§3.5.2) and the cache update "upon receipt of a
ReserveNow.conf" (§5.13, which reads like an Authorize.conf) are not sent / done.

Firmware Management (M7, §4.4, §4.5, §5.9, §5.19, errata 3.12 / 3.31 / 3.47): the steps run in
``runtime.maintenance`` (see there for the shared interpretations). UpdateFirmware: Downloading -> Downloaded ->
[connectors without a transaction Unavailable until none is left] -> Installing -> reboot (as a Hard Reset) ->
BootNotification -> Installed; failure injection ends it with DownloadFailed / InstallationFailed. GetDiagnostics:
Uploading -> Uploaded / UploadFailed. TriggerMessage Firmware- / DiagnosticsStatusNotification is Accepted and sends
the current step while an update / upload runs, else Idle (§4.4 / §4.5).

Interpretations (M7, not defined by the spec): an UpdateFirmware while another update has not started installing
replaces it (no status is sent for the old one); one received while Installing is ignored (the conf has no status
to refuse it). A GetDiagnostics while an upload runs replaces it. GetDiagnostics always has diagnostics available
and answers the file name ``<charger id>-diagnostics-<UTC time>.log``; ``startTime`` / ``stopTime`` are not used.
The update survives reconnects but not a stop of the charger. A Reset during an update does not cancel it.
While an update waits for its retrieveDate nothing has been reported yet (1.6 has no DownloadScheduled), so a
triggered FirmwareStatusNotification sends Idle.

Concurrency invariant: there is no lock around connector state. Every state check and the mutation that depends
on it run without an ``await`` in between (single-threaded asyncio), so they are atomic.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from ...charging import power
from ...charging.power import PowerState
from ...rpc import CallError, wait_event
from ...runtime.local_auth import AuthCache, LocalAuthList, Reservation
from ...runtime.maintenance import FirmwareJob, LogJob, Maintenance
from .. import base as d
from ..base import AuthResult as A
from ..base import DomainStatus as S
from ..base import StopReason as R
from ..station import StationHost, VersionStation
from .adapter import Adapter16
from .composite import ActiveTransaction, CompositePeriod, composite_schedule, convert, limit_at
from .config_store import ConfigStore
from .enums import ChargePointErrorCode
from .profiles import ChargingProfile, ChargingRateUnit, ClearStatus, InstalledProfile, ProfileStatus, ProfileStore
from .state import ConnectorState, IllegalTransition

log = logging.getLogger(__name__)

# Measurand names as used in the MeterValues*SampledData / StopTxn*Data configuration values.
ENERGY = "Energy.Active.Import.Register"
POWER = "Power.Active.Import"
CURRENT = "Current.Import"
VOLTAGE = "Voltage"
SOC = "SoC"

#: The firmware steps 1.6 ``FirmwareStatus`` can report (M7).
FIRMWARE_STATES = frozenset({d.FirmwareState.DOWNLOADING, d.FirmwareState.DOWNLOADED, d.FirmwareState.DOWNLOAD_FAILED,
                             d.FirmwareState.INSTALLING, d.FirmwareState.INSTALLED,
                             d.FirmwareState.INSTALLATION_FAILED})
POWER_DECIMALS = 1  # same resolution as the GetCompositeSchedule limits
CURRENT_DECIMALS = 3
SOC_DECIMALS = 1
LOCATION = "Outlet"
SOC_LOCATION = "EV"


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
CACHE_ENABLED_KEY = "AuthorizationCacheEnabled"
LOCAL_PRE_AUTHORIZE_KEY = "LocalPreAuthorize"
LOCAL_AUTHORIZE_OFFLINE_KEY = "LocalAuthorizeOffline"
LIST_ENABLED_KEY = "LocalAuthListEnabled"
LIST_MAX_KEY = "LocalAuthListMaxLength"
SEND_LIST_MAX_KEY = "SendLocalListMaxLength"
RESERVE_ZERO_KEY = "ReserveConnectorZeroSupported"


def _reservation_view(r: Reservation) -> Dict[str, Any]:
    return {"id": r.id, "connector": r.connector, "id_tag": r.id_token, "parent_id_token": r.parent_id_token,
            "expiry": r.expiry.isoformat()}


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
    reservation_id: Optional[int] = None  # the reservation this transaction used (StartTransaction.req)


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
    pending_reservation: Optional[int] = None  # reservation used by ``pending_tag``
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


class Station16(VersionStation):
    #: 1.6 ``ChargePointErrorCode`` values an operator may set (``NoError`` is the absence of a fault).
    fault_codes = tuple(e.value for e in ChargePointErrorCode if e is not ChargePointErrorCode.no_error)

    def __init__(self, host: StationHost, *, strict: bool = False) -> None:
        super().__init__(host, strict=strict)
        cfg = self.cfg = host.cfg
        self.ev, self.supply = host.ev, host.supply
        self.profiles = ProfileStore(cfg.connectors)
        self._adapter = Adapter16(strict=strict)
        names = {k.name for k in self._adapter.config_keys}
        derived = {CONNECTORS_KEY: str(cfg.connectors)}
        if cfg.meter_sample_interval_s is not None:
            derived[SAMPLE_KEY] = str(cfg.meter_sample_interval_s)
        overrides = {k: v for k, v in derived.items() if k in names}
        overrides.update(cfg.config_keys)
        self.store = ConfigStore(self._adapter.config_keys, overrides)

        self.cp_state = ConnectorState(0)  # connectorId 0: the charge point as a whole
        self._station_operative = True
        self._station_pending: Optional[bool] = None
        self.conns: Dict[int, _Conn] = {i: _Conn(i, ConnectorState(i)) for i in range(1, cfg.connectors + 1)}
        #: (vendorId, messageId or None) -> answer for inbound DataTransfer; default UnknownVendorId.
        self.data_transfer_rules: Dict[Tuple[str, Optional[str]], d.DataTransferResult] = {}
        #: Local Authorization List / Authorization Cache / reservations by id (M6).
        self.local_list = LocalAuthList(self.store.get_int(LIST_MAX_KEY))
        self.cache = AuthCache()
        self.reservations: Dict[int, Reservation] = {}
        #: Firmware update / diagnostics upload (M7).
        self.maint = Maintenance(
            cfg.maintenance, clock=lambda: self.host.clock(), firmware_states=FIRMWARE_STATES,
            send_firmware=lambda ev: self._maint_send(self._adapter.send_firmware_status(ev),
                                                      "FirmwareStatusNotification"),
            send_log=lambda ev: self._maint_send(self._adapter.send_diagnostics_status(ev),
                                                 "DiagnosticsStatusNotification"),
            busy=lambda: any(c.tx is not None for c in self.conns.values()),
            hold_changed=self._refresh_all, reboot=functools.partial(self._do_reset, d.ResetKind.HARD))

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
            d.SendLocalList: self._send_local_list,
            d.GetLocalListVersion: lambda cmd: (self.local_list.version if self.store.get_bool(LIST_ENABLED_KEY)
                                                else 0),
            d.ClearCache: self._clear_cache,
            d.ReserveNow: self._reserve_now,
            d.CancelReservation: self._cancel_reservation,
            d.UpdateFirmware: self._update_firmware,
            d.GetDiagnostics: self._get_diagnostics,
        }

    # ================================================================== session (called by the shell)

    @property
    def adapter(self) -> Adapter16:
        return self._adapter

    @property
    def connector_count(self) -> int:
        return len(self.conns)

    async def on_online(self) -> None:
        # Bring every state up to date first (a transaction may have ended while offline), then report.
        self._apply(self.cp_state, self._station_target())
        for c in self.conns.values():
            self._apply(c.state, self._target(c), c.fault)
        await self._send_status(self.cp_state)
        for c in self.conns.values():
            await self._send_status(c.state)
        self.host.spawn(self._outbox_worker())
        self.host.start_ticks()
        for c in self.conns.values():
            if c.tx is not None and c.tx.tx_id is not None:
                self._start_meter(c)
        for r in list(self.reservations.values()):
            if r.timer is None:  # cancelled by close(): the charger was stopped and started again
                self._arm(r)
        self.maint.on_online()

    def on_session_ended(self) -> None:
        for c in self.conns.values():
            c.meter_task = None  # cancelled with the other session tasks
        self._outq_session_ended()

    async def wait_heartbeat(self) -> bool:
        return await self._wait_interval(HEARTBEAT_KEY, self.store.get_int(HEARTBEAT_KEY))

    def set_heartbeat_interval(self, seconds: int) -> None:
        self.store.set(HEARTBEAT_KEY, str(seconds))

    def snapshot(self) -> Dict[str, Any]:
        def conn(c: _Conn) -> Dict[str, Any]:
            tx = None
            if c.tx is not None:
                tx = {"tx_id": c.tx.tx_id, "id_tag": c.tx.id_tag, "meter_start_wh": c.tx.meter_start_wh,
                      "started_at": c.tx.started_at.isoformat()}
            r = self._specific(c)
            return {"id": c.id, "status": c.state.status.value, "error_code": c.state.error_code,
                    "plugged": c.plugged, "operative": c.operative, "pending_operative": c.pending_operative,
                    "pending_tag": c.pending_tag, "meter_wh": round(c.meter_wh, 3),
                    "power_w": round(c.pw.power_w, POWER_DECIMALS) if c.pw is not None else 0.0,
                    "soc_pct": round(c.pw.soc_pct, SOC_DECIMALS) if c.pw is not None else None,
                    "transaction": tx, "reservation": _reservation_view(r) if r is not None else None}

        return {
            "tx_queue": sum(1 for m in self._outq if m.transactional),
            "local_list_version": self.local_list.version,
            "reservations": [_reservation_view(r) for r in self.reservations.values()],
            "station": {"status": self.cp_state.status.value, "operative": self._station_operative},
            "connectors": [conn(c) for c in self.conns.values()],
            **self.maint.snapshot(),
        }

    # ================================================================== configuration changes

    def _on_config(self, key: str, value: str) -> None:
        ev = self._wake.get(key)
        if ev is not None:
            ev.set()
            self._wake[key] = asyncio.Event()

    async def _wait_interval(self, key: str, interval: int) -> bool:
        """Wait ``interval`` seconds (until changed when 0 = disabled). True when ``key`` changed meanwhile."""
        return await wait_event(self._wake[key], interval if interval > 0 else None)

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
                    self.host.abort_session()
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
            if msg.result is not None and not msg.result.done():
                msg.result.set_result(A.ERROR)
            if msg.kind is _Kind.START and msg.conn is not None and msg.tx is not None and msg.tx.tx_id is None:
                try:  # the answer was not applied: do not leave the connector stuck in Preparing
                    self._abort_tx(msg.conn, msg.tx, finishing=False)
                except Exception:  # noqa: BLE001
                    log.exception("%s: detaching the failed transaction failed", self.cfg.id)
            self.host.abort_session(repr(e))
            return False

    def _delivered(self, msg: _OutMsg, value: Any) -> None:
        if msg.kind is _Kind.START:
            assert msg.conn is not None and msg.tx is not None
            outcome = self._on_started(msg.conn, msg.tx, value)
            if msg.result is not None and not msg.result.done():
                msg.result.set_result(outcome)
        elif msg.kind is _Kind.STOP and msg.tx is not None:
            self._remember(msg.tx.id_tag, value)  # §3.5.1: StopTransaction.conf idTagInfo

    # ================================================================== status

    def _usable(self, c: _Conn) -> bool:
        """Operative and not held Unavailable by a firmware update waiting to install (``runtime.maintenance``)."""
        return c.operative and not (self.maint.hold and c.tx is None)

    def _target(self, c: _Conn) -> S:
        if c.fault:
            return S.FAULTED
        if not self._usable(c):
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
        if self._specific(c) is not None or self._held_for_zero(c):
            return S.RESERVED
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
        """Move ``c`` to its target state; when online, queue the StatusNotification (never waits). With
        connector-0 reservations the other connectors may change too (Reserved while held for them)."""
        self._refresh_one(c)
        if self._general():
            self._refresh_all(skip=c)

    def _refresh_one(self, c: _Conn) -> None:
        if self._apply(c.state, self._target(c), c.fault) and self.host.online:
            self._queue_status(c.state)

    def _refresh_all(self, skip: Optional[_Conn] = None) -> None:
        for c in self.conns.values():
            if c is not skip:
                self._refresh_one(c)

    def _refresh_station(self) -> None:
        if self._apply(self.cp_state, self._station_target()) and self.host.online:
            self._queue_status(self.cp_state)

    def _status_event(self, st: ConnectorState) -> d.ConnectorStatus:
        return d.ConnectorStatus(st.connector_id, st.connector_id, st.status, st.error_code, st.info,
                                 st.vendor_error_code, self.host.clock())

    def _queue_status(self, st: ConnectorState) -> None:
        ev = self._status_event(st)  # captured now: the state may change before it is sent
        self._outq_push(_OutMsg(_Kind.STATUS, lambda: self._adapter.send_connector_status(ev)))

    async def _send_status(self, st: ConnectorState) -> None:
        await self.host.safe(self._adapter.send_connector_status(self._status_event(st)), "StatusNotification")

    # ================================================================== authorization / transactions

    async def _authorize(self, tag: str) -> A:
        return (await self._authorize_info(tag))[0].status

    def _known(self, tag: str) -> Optional[d.IdTokenInfo]:
        """The tag's Local Authorization List entry (when enabled; priority, §3.5.3), else its cache entry (when
        enabled). An Accepted list entry past its expiryDate counts as Expired (§3.5.4)."""
        now = self.host.clock()
        if self.store.get_bool(LIST_ENABLED_KEY):
            entry = self.local_list.get(tag)
            if entry is not None:
                return replace(entry, status=A.EXPIRED) if entry.accepted and entry.expired(now) else entry
        if self.store.get_bool(CACHE_ENABLED_KEY):
            return self.cache.get(tag, now)
        return None

    def _remember(self, tag: str, info: Optional[d.IdTokenInfo]) -> None:
        """§3.5.1: a received IdTagInfo updates the cache, except for a tag in the Local Authorization List
        (§3.5.3)."""
        if info is not None and self.store.get_bool(CACHE_ENABLED_KEY) and self.local_list.get(tag) is None:
            self.cache.update(tag, info, self.host.clock())

    async def _authorize_info(self, tag: str) -> Tuple[d.IdTokenInfo, bool]:
        """Authorize ``tag`` (module docstring): ``(info, asked)``, ``asked`` = the info is a CSMS answer."""
        mode = self.cfg.auth_mode
        if mode == "accept_all":
            return d.IdTokenInfo(A.ACCEPTED), False
        known = self._known(tag)
        local = tag in self.cfg.id_tags
        if mode == "local_only":
            if local:
                return d.IdTokenInfo(A.ACCEPTED), False
            return (known if known is not None else d.IdTokenInfo(A.INVALID)), False
        if mode == "local_then_csms" and local:
            return d.IdTokenInfo(A.ACCEPTED), False
        if not self.host.online:
            if known is not None and self.store.get_bool(LOCAL_AUTHORIZE_OFFLINE_KEY):
                return known, False
            # TODO(spec): AllowOfflineTxForUnknownId (§3.5.4) is not simulated.
            return d.IdTokenInfo(A.OFFLINE), False
        if known is not None and known.accepted and self.store.get_bool(LOCAL_PRE_AUTHORIZE_KEY):
            return known, False
        info: Optional[d.IdTokenInfo] = await self.host.safe(self._adapter.send_authorize(d.AuthorizeRequest(tag)),
                                                         "Authorize")
        self._remember(tag, info)
        return (info if info is not None else d.IdTokenInfo(A.ERROR)), True

    # ================================================================== reservations (M6)

    def _active(self) -> List[Reservation]:
        """Reservations that hold a connector: not claimed by a waiting tag (``runtime.local_auth``)."""
        return [r for r in self.reservations.values() if r.claimed_by is None]

    def _specific(self, c: _Conn) -> Optional[Reservation]:
        return next((r for r in self._active() if r.connector == c.id), None)

    def _general(self) -> List[Reservation]:
        """Connector-0 reservations."""
        return [r for r in self._active() if r.connector == 0]

    def _free(self, c: _Conn) -> bool:
        """``c`` could serve a connector-0 reservation now: no fault, operative, no transaction, no waiting tag,
        not reserved itself. A plugged cable does not count (it authorizes nothing; module docstring)."""
        return (not c.fault and self._usable(c) and c.tx is None and c.pending_tag is None
                and self._specific(c) is None)

    def _held_for_zero(self, c: _Conn) -> bool:
        """``c`` is free and the free connectors are no more than the connector-0 reservations."""
        n = len(self._general())
        return n > 0 and self._free(c) and sum(1 for x in self.conns.values() if self._free(x)) <= n

    def _reservation_for(self, c: _Conn, tag: str, parent: Optional[str]) -> Tuple[bool, Optional[Reservation]]:
        """May ``tag`` (with ``parent``) start on ``c``; the reservation it uses, if any (§5.13)."""
        r = self._specific(c)
        if r is not None:
            return (True, r) if r.matches(tag, parent) else (False, None)
        general = self._general()
        mine = next((x for x in general if x.matches(tag, parent)), None)
        if mine is not None:
            return True, mine
        # §5.13 "one connector remains available": refused only when this start takes the last free connector(s).
        left = sum(1 for x in self.conns.values() if self._free(x)) - (1 if self._free(c) else 0)
        return left >= len(general), None

    def _parent_may_help(self, c: _Conn, tag: str) -> bool:
        """The reservations relevant for ``c`` refuse ``tag`` by itself, but one has a parentIdTag."""
        r = self._specific(c)
        return any(x.parent_id_token for x in ([r] if r is not None else self._general()))

    async def _parent_of(self, tag: str, info: d.IdTokenInfo, asked: bool) -> Optional[str]:
        """The tag's parent: from its authorization, the Local Authorization List or the cache, else from an
        Authorize.req (§5.13, errata 4.12) unless the CSMS was asked already or may not be asked."""
        if info.parent_id_token:
            return info.parent_id_token
        known = self._known(tag)
        if known is not None and known.parent_id_token:
            return known.parent_id_token
        if asked or not self.host.online or self.cfg.auth_mode in ("accept_all", "local_only"):
            return None
        res: Optional[d.IdTokenInfo] = await self.host.safe(self._adapter.send_authorize(d.AuthorizeRequest(tag)),
                                                        "Authorize")
        self._remember(tag, res)
        return res.parent_id_token if res is not None else None

    def _claim(self, c: _Conn, r: Optional[Reservation]) -> None:
        """The tag waiting on ``c`` claims reservation ``r``; it is consumed when the transaction starts
        (:meth:`_consume`) and released when the tag is dropped (:meth:`_release`)."""
        if r is None or self.reservations.get(r.id) is not r:
            return
        r.claimed_by, c.pending_reservation = c.id, r.id
        self._refresh_all(skip=c)  # ``c`` itself is refreshed by the caller

    def _release(self, c: _Conn) -> None:
        """The waiting tag of ``c`` is dropped: its reservation (if still there) holds its connector again."""
        rid, c.pending_reservation = c.pending_reservation, None
        r = self.reservations.get(rid) if rid is not None else None
        if r is not None and r.claimed_by == c.id:
            r.claimed_by = None

    def _consume(self, c: _Conn) -> Optional[int]:
        """The transaction on ``c`` starts: its claimed reservation ends; its id for StartTransaction.req."""
        rid, c.pending_reservation = c.pending_reservation, None
        r = self.reservations.get(rid) if rid is not None else None
        if r is None or r.claimed_by != c.id:
            return None  # expired / cancelled meanwhile
        del self.reservations[r.id]
        r.cancel_timer()
        return rid

    def _arm(self, r: Reservation) -> None:
        r.cancel_timer()
        delay = (r.expiry - self.host.clock()).total_seconds()
        r.timer = asyncio.get_running_loop().call_later(max(0.0, delay), self._reservation_expired, r)

    def close(self) -> None:
        """The charger stops: cancel the reservation expiry timers (re-armed by :meth:`on_online`)."""
        for r in self.reservations.values():
            r.cancel_timer()
        self.maint.close()

    def _drop_reservation(self, r: Reservation, why: str) -> None:
        if self.reservations.get(r.id) is not r:
            return
        del self.reservations[r.id]
        r.cancel_timer()
        log.info("%s: reservation %d %s", self.cfg.id, r.id, why)
        self._refresh_all()

    def _drop_reservations_of(self, conns: Iterable[_Conn], *, station: bool = False) -> None:
        """§5.13 (3): the connector (connector 0: the charge point) became Faulted or Unavailable."""
        ids = {c.id for c in conns} | ({0} if station else set())
        for r in [r for r in self.reservations.values() if r.connector in ids]:
            self._drop_reservation(r, "ended: its connector is faulted / unavailable")

    def _reservation_expired(self, r: Reservation) -> None:
        r.timer = None
        self._drop_reservation(r, "expired")

    def _reserve_now(self, cmd: d.ReserveNow) -> d.ReservationStatus:
        RS = d.ReservationStatus
        if cmd.evse_id == 0 and not self.store.get_bool(RESERVE_ZERO_KEY):
            return RS.REJECTED
        if cmd.expiry <= self.host.clock():
            return RS.REJECTED
        old = self.reservations.pop(cmd.reservation_id, None)  # checked as if it did not exist
        status = self._reservation_status(cmd.evse_id)
        if status is not RS.ACCEPTED:
            if old is not None:
                self.reservations[old.id] = old
            return status
        if old is not None:
            old.cancel_timer()
        r = Reservation(cmd.reservation_id, cmd.evse_id, cmd.id_token, cmd.expiry, cmd.parent_id_token)
        self._arm(r)
        self.reservations[r.id] = r
        self.host.after_reply(self._refresh_all)  # Reserved after the answer
        return RS.ACCEPTED

    def _reservation_status(self, connector: int) -> d.ReservationStatus:
        RS = d.ReservationStatus
        if connector == 0:
            if sum(1 for c in self.conns.values() if self._free(c)) > len(self._general()):
                return RS.ACCEPTED
            if all(c.fault for c in self.conns.values()):
                return RS.FAULTED
            if all(not self._usable(c) for c in self.conns.values()):
                return RS.UNAVAILABLE
            return RS.OCCUPIED
        c = self.conns[connector]
        if c.fault:
            return RS.FAULTED
        if not self._usable(c):
            return RS.UNAVAILABLE
        if c.plugged or not self._free(c) or self._held_for_zero(c):
            return RS.OCCUPIED
        return RS.ACCEPTED

    def _cancel_reservation(self, cmd: d.CancelReservation) -> d.CommandStatus:
        r = self.reservations.pop(cmd.reservation_id, None)
        if r is None:
            return d.CommandStatus.REJECTED
        r.cancel_timer()
        self.host.after_reply(self._refresh_all)  # Available after the answer
        return d.CommandStatus.ACCEPTED

    # ================================================================== local authorization list / cache (M6)

    def _send_local_list(self, cmd: d.SendLocalList) -> d.LocalListStatus:
        if not self.store.get_bool(LIST_ENABLED_KEY):
            return d.LocalListStatus.NOT_SUPPORTED
        if len(cmd.entries) > self.store.get_int(SEND_LIST_MAX_KEY):
            return d.LocalListStatus.FAILED
        self.local_list.max_entries = self.store.get_int(LIST_MAX_KEY)
        return d.LocalListStatus(self.local_list.update(cmd.version, cmd.full, cmd.entries).value)

    def _clear_cache(self, cmd: d.ClearCache) -> d.CommandStatus:
        if not self.store.get_bool(CACHE_ENABLED_KEY):
            return d.CommandStatus.REJECTED
        self.cache.clear()
        return d.CommandStatus.ACCEPTED

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
        return limit_at(self.profiles, c.id, self.host.clock(), transactions=self._transactions(),
                        number_phases=self._phases(c.id), voltage=self.supply.phase_voltage_v())

    def _advance(self, c: _Conn) -> None:
        """Step the power model of a charging connector up to now (power, energy register, SoC, suspension) and
        restart the interval; a connector that is not charging only restarts the interval."""
        now = self.host.monotonic()
        if c.pw is not None and not self.host.paused:  # paused: frozen, the interval just restarts (no catch-up)
            dt = max(0.0, now - c.last_mono) if c.last_mono is not None else 0.0
            c.pw = self.host.step_power(c.pw, dt, self._limit_w(c))
            c.meter_wh = c.pw.energy_wh
        c.last_mono = now

    def settle(self) -> None:
        """Integrate every charging connector up to now (before a limit change)."""
        for c in self.conns.values():
            self._advance(c)

    def tick(self) -> None:
        for c in self.conns.values():
            if c.pw is not None:
                self._advance(c)
                self._refresh(c)  # Charging <-> SuspendedEV / SuspendedEVSE

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

    async def _start_tx(self, c: _Conn, tag: str, profile: Optional[ChargingProfile] = None,
                        reservation: Optional[Reservation] = None) -> A:
        """Start a transaction for an authorized ``tag``; the state is re-checked here (an await may lie between
        authorization and this call). Not plugged = keep the tag and wait for the cable (Accepted). ``reservation``
        is the one the tag uses (it ends once the tag holds the connector); a waiting tag keeps its reservationId
        in ``pending_reservation``.

        No await until the StartTransaction item is queued: checks and changes below are atomic."""
        if c.tx is not None:
            return A.BUSY
        if c.fault or not self._usable(c):
            return A.UNAVAILABLE
        c.finishing = False
        if not c.plugged:
            if c.pending_tag != tag:
                self._release(c)  # another tag's claim does not carry over
            c.pending_tag, c.pending_profile = tag, profile
            self._claim(c, reservation)
            self._refresh(c)
            return A.ACCEPTED
        if not self.host.online:
            # TODO(spec): offline transactions are not simulated in v0.1 (§3.7 p.17 would queue a
            # StartTransaction without transactionId); the start is refused with Offline.
            self._clear_pending(c)
            self._refresh(c)
            return A.OFFLINE
        self._claim(c, reservation)
        reservation_id = self._consume(c)
        self._clear_pending(c)
        tx = c.tx = _Tx(tag, int(round(c.meter_wh)), self.host.clock(), profile=profile,
                        reservation_id=reservation_id)
        c.last_mono = self.host.monotonic()
        self._refresh(c)  # Preparing
        ev = d.TxStarted(c.id, tx.id_tag, tx.meter_start_wh, tx.started_at, reservation_id=reservation_id)
        result: asyncio.Future = asyncio.get_running_loop().create_future()
        self._outq_push(_OutMsg(_Kind.START, lambda: self._adapter.send_tx_started(ev), c, tx, result))
        # The caller may be cancelled here: the item stays queued and the worker finishes the start.
        return await result

    def _on_started(self, c: _Conn, tx: _Tx, res: Optional[d.TxStartedResult]) -> A:
        """Apply the StartTransaction answer (worker context, synchronous); returns the caller's outcome."""
        if res is None:  # discarded after TransactionMessageAttempts failures, or dropped
            self._abort_tx(c, tx, finishing=False)
            return A.ERROR
        self._remember(tx.id_tag, res.id_token_info)  # §3.5.1: StartTransaction.conf idTagInfo
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
            c.meter_task = self.host.spawn(self._meter_loop(c))

    async def _meter_loop(self, c: _Conn) -> None:
        # TODO(spec): clock-aligned readings (ClockAlignedDataInterval / MeterValuesAlignedData) are not sent yet.
        while c.tx is not None:
            if await self._wait_interval(SAMPLE_KEY, self.store.get_int(SAMPLE_KEY)):
                continue  # interval changed: restart the wait with the new value
            tx = c.tx
            if tx is None or tx.tx_id is None:
                continue
            self._advance(c)
            ev = d.TxUpdated(c.id, tx.tx_id, self.host.clock(), self._samples(c, self.store.get_csl(SAMPLED_DATA_KEY)))
            self._outq_push(_OutMsg(_Kind.METER, lambda ev=ev: self._adapter.send_tx_updated(ev), c, tx))

    def _end_tx(self, c: _Conn, reason: R, *, tx: Optional[_Tx] = None) -> None:
        """End the running transaction (only if it is ``tx`` when given). Never waits for the CSMS."""
        cur = c.tx
        if cur is not None and (tx is None or cur is tx):
            self._end_tx_now(c, reason, cur)

    def _end_tx_now(self, c: _Conn, reason: R, tx: _Tx) -> None:
        self._advance(c)
        tx.ended = _Stop(int(round(c.meter_wh)), self.host.clock(), reason,
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
        self.maint.wake()

    def _stop_msg(self, c: _Conn, tx: _Tx) -> _OutMsg:
        st = tx.ended
        assert st is not None and tx.tx_id is not None
        ev = d.TxEnded(c.id, tx.tx_id, st.meter_stop_wh, st.timestamp, st.reason, tx.id_tag, st.samples)
        return _OutMsg(_Kind.STOP, lambda: self._adapter.send_tx_ended(ev), c, tx)

    # ================================================================== CSMS commands

    async def handle(self, cmd: d.Command) -> Any:
        fn = self._commands.get(type(cmd))
        return fn(cmd) if fn is not None else d.CommandStatus.NOT_IMPLEMENTED

    def _set_profile(self, cmd: d.SetProfile) -> d.CommandStatus:
        c = self.conns.get(cmd.evse_id)
        active = int(c.tx.tx_id) if c is not None and c.tx is not None and c.tx.tx_id is not None else None
        self.settle()
        if self._install(cmd.evse_id, cmd.profile, active_transaction=active) is not ProfileStatus.ACCEPTED:
            return d.CommandStatus.REJECTED
        self.host.after_reply(self.tick)
        return d.CommandStatus.ACCEPTED

    def _clear_profiles(self, cmd: d.ClearProfiles) -> d.CommandStatus:
        self.settle()
        st = self.profiles.clear(profile_id=cmd.profile_id, connector_id=cmd.evse_id, purpose=cmd.purpose,
                                 stack_level=cmd.stack_level)
        if st is not ClearStatus.ACCEPTED:
            return d.CommandStatus.UNKNOWN
        self.host.after_reply(self.tick)
        return d.CommandStatus.ACCEPTED

    def _get_composite(self, cmd: d.GetComposite) -> d.GetCompositeResult:
        if not 0 <= cmd.evse_id <= self.connector_count or cmd.duration < 1:
            return d.GetCompositeResult(d.CommandStatus.REJECTED)
        now = self.host.clock()
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

    def configuration(self) -> List[d.ConfigEntry]:
        known, _ = self.store.get()
        return known

    def _get_config(self, cmd: d.GetConfig) -> d.GetConfigResult:
        known, unknown = self.store.get(cmd.keys)
        return d.GetConfigResult(known, unknown)

    def _reset(self, cmd: d.Reset) -> d.CommandStatus:
        self.host.after_reply(functools.partial(self._do_reset, cmd.kind))
        return d.CommandStatus.ACCEPTED

    def _unlock(self, cmd: d.Unlock) -> d.UnlockStatus:
        c = self.conns[cmd.evse_id]
        if c.tx is not None:
            self.host.after_reply(functools.partial(self._end_tx, c, R.UNLOCK_COMMAND, tx=c.tx))
        return d.UnlockStatus.UNLOCKED  # TODO(spec): UnlockFailed / NotSupported answers are not configurable yet

    def _data_transfer(self, cmd: d.DataTransfer) -> d.DataTransferResult:
        rule = (self.data_transfer_rules.get((cmd.vendor_id, cmd.message_id))
                or self.data_transfer_rules.get((cmd.vendor_id, None)))
        return rule or d.DataTransferResult(d.DataTransferStatus.UNKNOWN_VENDOR_ID)

    def _remote_start(self, cmd: d.RequestStart) -> d.CommandStatus:
        if not self.host.online:
            return d.CommandStatus.REJECTED  # TODO(spec): RemoteStart before an accepted boot is rejected

        def free(c: _Conn) -> bool:
            return (c.tx is None and self._usable(c) and not c.fault and c.pending_tag is None
                    and self._reservation_for(c, cmd.id_token, None)[0])

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
        self._claim(c, self._reservation_for(c, cmd.id_token, None)[1])  # consumed when the transaction starts
        self.host.after_reply(functools.partial(self._remote_start_go, c, cmd.id_token, cmd.profile))
        return d.CommandStatus.ACCEPTED

    async def _remote_start_go(self, c: _Conn, tag: str, profile: Optional[ChargingProfile] = None) -> None:
        if self.store.get_bool(AUTHORIZE_REMOTE_KEY) and await self._authorize(tag) is not A.ACCEPTED:
            if c.pending_tag == tag:  # still our claim (the await may have let another start in)
                self._clear_pending(c)
                self._refresh(c)
            return
        # TODO(spec): ConnectionTimeOut (cable not plugged in time) is not enforced.
        await self._start_tx(c, tag, profile)

    def _remote_stop(self, cmd: d.RequestStop) -> d.CommandStatus:
        for c in self.conns.values():
            if c.tx is not None and c.tx.tx_id == cmd.tx_id:
                self.host.after_reply(functools.partial(self._end_tx, c, R.REMOTE, tx=c.tx))
                return d.CommandStatus.ACCEPTED
        return d.CommandStatus.REJECTED

    async def _do_reset(self, kind: d.ResetKind) -> None:
        # TODO(spec): both Soft and Hard reset stop running transactions before closing (reason SoftReset /
        # HardReset); a real Hard reset may send StopTransaction only after the reboot.
        try:
            reason = R.SOFT_RESET if kind is d.ResetKind.SOFT else R.HARD_RESET
            for c in self.conns.values():
                self._end_tx(c, reason)
                self._clear_pending(c)
            # Let the StopTransactions go out before closing, including one queued by a StartTransaction answer
            # that arrives meanwhile; bounded, a silent CSMS must not block the Reset (anything left stays queued
            # for the next session). Idle is set only when the queue is empty and no answer is being applied.
            try:
                async with asyncio.timeout(self.host.call_timeout):
                    while self._outq:
                        await self._outq_idle.wait()
            except TimeoutError:
                log.info("%s: Reset closes with %d queued message(s)", self.cfg.id, len(self._outq))
        finally:
            self.host.end_session_for_reset()

    def _trigger(self, cmd: d.Trigger) -> d.CommandStatus:
        what = cmd.what
        if what is d.TriggerWhat.BOOT:
            # TODO(spec): §5.17 TriggerMessage does not say what the answer to a triggered BootNotification
            # changes once the charger is registered; the shell keeps the registration of a triggered boot (a
            # Pending / Rejected answer would otherwise take an accepted charger offline with no boot loop running).
            if self.host.registration != d.BootStatus.ACCEPTED.value:
                self.host.kick_boot()
            else:
                self.host.after_reply(functools.partial(self.host.send_boot, triggered=True))
        elif what is d.TriggerWhat.HEARTBEAT:
            self.host.after_reply(self.host.heartbeat)
        elif what is d.TriggerWhat.STATUS:
            targets = ([self.cp_state] if cmd.evse_id in (None, 0) else []) + [
                c.state for c in self.conns.values() if cmd.evse_id in (None, c.id)]
            self.host.after_reply(functools.partial(self._send_statuses, targets))
        elif what is d.TriggerWhat.METER_VALUES:
            # TODO(spec): connectorId 0 / absent triggers a reading per connector (no main meter is simulated).
            targets = [c for c in self.conns.values() if cmd.evse_id in (None, 0, c.id)]
            self.host.after_reply(functools.partial(self._send_readings, targets))
        elif what is d.TriggerWhat.FIRMWARE_STATUS:
            # §4.5: Idle only when no update is running, else the current step.
            st = self.maint.firmware_status if self.maint.firmware_busy else None
            ev = d.FirmwareStatus(st or d.FirmwareState.IDLE)
            self.host.after_reply(functools.partial(self._maint_send, self._adapter.send_firmware_status(ev),
                                                    "FirmwareStatusNotification", triggered=True))
        elif what is d.TriggerWhat.DIAGNOSTICS_STATUS:
            st_log = self.maint.log_status if self.maint.log_busy else None
            ev_log = d.LogStatus(st_log or d.LogState.IDLE)
            self.host.after_reply(functools.partial(self._maint_send, self._adapter.send_diagnostics_status(ev_log),
                                                    "DiagnosticsStatusNotification", triggered=True))
        else:
            return d.CommandStatus.NOT_IMPLEMENTED
        return d.CommandStatus.ACCEPTED

    # ------------------------------------------------------------------ firmware / diagnostics (M7)

    async def _maint_send(self, aw: Awaitable[Any], what: str, *, triggered: bool = False) -> None:
        """Progress needs an accepted boot; a triggered status only a connection."""
        if not (self.host.connected if triggered else self.host.online):
            aw.close()  # type: ignore[attr-defined]
            log.info("%s: %s not sent (not online)", self.cfg.id, what)
            return
        await self.host.safe(aw, what)

    def _update_firmware(self, cmd: d.UpdateFirmware) -> None:
        if self.maint.installing:
            log.info("%s: UpdateFirmware ignored: a firmware update is installing", self.cfg.id)
            return
        job = FirmwareJob(cmd.location, cmd.retrieve_at, retries=cmd.retries, retry_interval_s=cmd.retry_interval_s)
        self.maint.cancel_firmware()
        self.host.after_reply(functools.partial(self.maint.start_firmware, job))

    def _get_diagnostics(self, cmd: d.GetDiagnostics) -> Optional[str]:
        name = f"{self.cfg.id}-diagnostics-{self.host.clock().strftime('%Y%m%dT%H%M%SZ')}.log"
        job = LogJob(cmd.location, name, retries=cmd.retries, retry_interval_s=cmd.retry_interval_s)
        self.maint.cancel_log()
        self.host.after_reply(functools.partial(self.maint.start_log, job))
        return name

    async def _send_statuses(self, states: List[ConnectorState]) -> None:
        for st in states:
            await self._send_status(st)

    async def _send_readings(self, conns: List[_Conn]) -> None:
        for c in conns:
            self._advance(c)
            tx_id = c.tx.tx_id if c.tx is not None else None
            ev = d.MeterSample(c.id, self.host.clock(), self._samples(c, self.store.get_csl(SAMPLED_DATA_KEY)),
                               "Trigger", tx_id)
            await self.host.safe(self._adapter.send_meter_sample(ev), "MeterValues")

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
                    self._clear_pending(c)
                changed.append(c)
        station = cmd.evse_id == 0
        if station:
            if scheduled:
                self._station_pending = cmd.operative
            else:
                self._station_operative, self._station_pending = cmd.operative, None
        if not cmd.operative:  # §5.13 (3): a reservation ends when its connector becomes Unavailable (after the
            # answer, like the StatusNotifications)
            self.host.after_reply(functools.partial(
                self._drop_reservations_of, [c for c in changed if not c.operative], station=station))
        self.host.after_reply(functools.partial(self._refresh_after_availability, station and not scheduled,
                                            changed))
        return d.AvailabilityStatus.SCHEDULED if scheduled else d.AvailabilityStatus.ACCEPTED

    def _refresh_after_availability(self, station: bool, changed: List[_Conn]) -> None:
        if station:
            self._refresh_station()
        for c in changed:
            self._refresh(c)

    # ================================================================== public API (Web API, M6)

    def _clear_pending(self, c: _Conn) -> None:
        """Drop the waiting tag; a reservation it claimed holds its connector again (the caller refreshes)."""
        self._release(c)
        c.pending_tag, c.pending_profile = None, None

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
        if c.pending_tag and self._usable(c) and not c.fault:
            await self._start_tx(c, c.pending_tag, c.pending_profile)  # with its pending_reservation
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
        if c.fault or not self._usable(c):
            return A.UNAVAILABLE
        if not self._reservation_for(c, id_tag, None)[0] and not self._parent_may_help(c, id_tag):
            return A.RESERVED  # reserved for another tag: refused without asking the CSMS
        info, asked = await self._authorize_info(id_tag)
        if info.status is not A.ACCEPTED:
            return info.status
        allowed, r = self._reservation_for(c, id_tag, None)
        if not allowed:
            parent = await self._parent_of(id_tag, info, asked)
            allowed, r = self._reservation_for(c, id_tag, parent)  # re-checked after the await
            if not allowed:
                return A.RESERVED
        return await self._start_tx(c, id_tag, reservation=r)

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
        self._drop_reservations_of([c])  # §5.13 (3)
        self._refresh(c)

    async def clear_fault(self, connector: int) -> bool:
        """Clear an operator fault; the connector goes back to Available / Preparing (plugged). ``False`` = it was
        not faulted."""
        c = self._conn(connector)
        if c.fault is None:
            return False
        c.fault = None
        self._refresh(c)
        return True

    def has_transaction(self, connector: int) -> bool:
        return self._conn(connector).tx is not None

    def faulted(self, connector: int) -> Optional[str]:
        """The operator fault's error code, or ``None``."""
        return self._conn(connector).fault

    def installed_profiles(self, connector: Optional[int] = None) -> Tuple[InstalledProfile, ...]:
        """Installed charging profiles (all, or those of ``connector``; 0 = the charge point)."""
        if connector:
            self._conn(connector)
        return self.profiles.profiles(connector)

    def composite(self, connector: int, duration: int, unit: Optional[str] = None) -> d.GetCompositeResult:
        """The GetCompositeSchedule answer for ``connector`` (0 = the charge point) right now, without a CSMS;
        ``unit`` is ``"W"`` / ``"A"`` (a :class:`ChargingRateUnit` works too), ``None`` = W."""
        return self._get_composite(d.GetComposite(connector, duration, ChargingRateUnit(unit) if unit else None))
