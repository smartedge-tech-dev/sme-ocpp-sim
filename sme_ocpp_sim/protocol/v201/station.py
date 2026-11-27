"""OCPP 2.0.1 station: connector states, authorization, the TransactionEvent flow (plan §11.2, M2) and the
CSMS remote control (M3: RequestStartTransaction / RequestStopTransaction / UnlockConnector / Reset /
ChangeAvailability / TriggerMessage).

One :class:`Station201` per 2.0.1 charging station, hosted by the shared shell ``runtime.charger.ChargerRuntime``
through :class:`~sme_ocpp_sim.protocol.station.StationHost`. It is a separate stack from ``protocol/v16``: it uses
only the 2.0.1 adapter, models and codec of this package and the version-neutral power model
(``charging.power`` through ``host.step_power``). Session tasks are started with ``host.spawn`` and end with the
session; transactions survive a disconnect.

Topology (plan §11.1 Q11): ``connectors: N`` is N EVSEs with one connector each, so EVSE ``i`` has connector 1
and the operator API's ``connector`` argument is the EVSE id. There is no StatusNotification for EVSE 0.

Transactions (2.0.1 E-series): the station generates the transaction id (a UUID4, 36 characters) and numbers the
TransactionEvents of one transaction with a continuous ``seqNo`` from 0: ``Started`` -> ``Updated`` x n ->
``Ended``. ``Started`` carries the id token, the EVSE and the meter start (``Transaction.Begin``); periodic
``Updated`` carry the configured readings (``Sample.Periodic``); a power-model suspension change sends an
``Updated`` with ``ChargingStateChanged``; ``Ended`` carries the final energy register (``Transaction.End``) and
``stoppedReason``.

Outbox: every TransactionEvent and every StatusNotification caused by a state change goes through ONE ordered
queue, sent by ONE worker per session that runs once the boot is accepted (as in the 1.6 station, but written
for 2.0.1). Enqueueing is synchronous, so the operator API never waits for the CSMS. A lost connection keeps the
head item queued for the next session; a CALLERROR / timeout / invalid answer of a TransactionEvent is retried
:data:`TX_MESSAGE_ATTEMPTS` times, :data:`TX_MESSAGE_RETRY_INTERVAL_S` x failures apart. StatusNotifications are
not retried and are dropped at the end of a session (the state is reported afresh after the next accepted boot).
The one exception to the single queue: the state report right after an accepted boot is sent directly, before the
worker starts, so it precedes the events queued while offline / Pending.

Remote control (M3): the adapter hands each remote-control request over as its 2.0.1 request model and gets the
2.0.1 response model back (:meth:`Station201.handle`). The answer is decided synchronously; what the request
causes (a transaction start / stop, a StatusNotification, a triggered message, the Reset) runs after the
CALLRESULT went out (``host.after_reply``), so the CSMS always sees the answer first.

Interpretations (not defined by the spec)
-----------------------------------------
- Transaction start / stop points: a transaction starts when the EV is connected AND the id token is authorized
  (whichever comes last is the ``triggerReason``: ``CablePluggedIn`` or ``Authorized``; TxStartPoint
  ``PowerPathClosed``) and charging begins at once. It ends on: the same token presented again or the operator
  stop (``StopAuthorized`` / ``Local``; the token is sent only when one was presented), cable unplugged
  (``EVCommunicationLost`` / ``EVDisconnected``), an operator fault (``AbnormalCondition`` / the fault code, which
  is a ``ReasonEnumType`` value), or a CSMS that does not accept the token in a TransactionEvent answer
  (``Deauthorized`` / ``DeAuthorized``, the default of ``StopTxOnInvalidId``).
- A token authorized before the cable is plugged waits on the connector, which stays ``Available`` until the
  cable comes (2.0.1 has no ``Preparing``); the Web UI still sees the 1.6-style domain status. An unplug drops a
  waiting token (the next session needs a fresh authorization).
- ``seqNo`` restarts at 0 for every transaction. A queued periodic ``Updated`` dropped by the queue bound
  (:data:`MAX_TX_QUEUE`) leaves a gap in ``seqNo``; ``Started`` / ``Ended`` / ``ChargingStateChanged`` are never
  dropped by the bound.
- ``offline`` is ``true`` for an event raised while the station had no WebSocket connection. An event raised while
  connected but before the boot was accepted (Pending) is held back until Accepted and sent with
  ``offline=false``. Queued events keep the flag they were raised with, also when they are resent after a
  reconnect; an event whose answer was lost with the connection is sent again (the CSMS can de-duplicate by
  ``transactionId`` + ``seqNo``).
- While the boot is not accepted only BootNotification is sent (no Authorize, StatusNotification or
  TransactionEvent; B02 / B03). CSMS CALLs are answered in every registration state: DataTransfer by the
  configured rules, every other action NotImplemented. A transaction may start while offline / Pending when the
  token is authorized locally (``accept_all``, ``local_only``, or a listed tag with ``local_then_csms``);
  ``csms_only`` answers ``Offline`` then.
- The operator API returns once ``Started`` is queued (2.0.1 needs no CSMS answer to know the transaction id); a
  later refusal of the token in the answer ends the transaction (see above).
- Periodic ``Updated`` interval: ``meter_sample_interval_s`` of the charger (0 = none), default
  :data:`DEFAULT_TX_UPDATED_INTERVAL_S`; readings: :data:`TX_UPDATED_MEASURANDS`. The Device Model variables
  (SampledDataCtrlr, OCPPCommCtrlr, ...) arrive with M4; ``config_keys`` (the 1.6 key table) is not used.
- Operator fault codes are the fault-type values of ``ReasonEnumType`` (:attr:`Station201.fault_codes`): a 2.0.1
  StatusNotification has no error code, so the code is only the ``stoppedReason`` of an interrupted transaction
  (NotifyEvent reporting arrives with the Device Model, M4).
- Smart charging is M5: no charging profiles are installed and no limit applies (the hardware maximum only).
- RequestStartTransaction (M3): the CSMS has authorized the token, so it is not authorized again
  (AuthorizeRemoteStart = false until the Device Model, M4). Without ``evseId`` the first startable EVSE with a
  plugged cable is used, else the first startable one. A faulted, inoperative or busy EVSE (or none free) is
  ``Rejected``; so is a ``chargingProfile`` that is not a ``TxProfile`` or that carries a ``transactionId``. An
  accepted ``chargingProfile`` is not applied before smart charging (M5). An accepted token waits on an unplugged
  EVSE like a local one; the transaction starts at once when the cable is plugged (``triggerReason``
  ``RemoteStart``) or when it comes (``CablePluggedIn``). ``Started`` carries the token with the ``type`` the CSMS
  sent and ``remoteStartId`` in ``transactionInfo``; the answer has no ``transactionId`` (no transaction exists
  before the request with these start points). ``groupIdToken`` is not used. A later RequestStartTransaction for
  an EVSE whose token still waits replaces the waiting token.
- RequestStopTransaction: ``Accepted`` for a running transaction of this station (it ends with ``RemoteStop`` /
  ``Remote``), ``Rejected`` for any other ``transactionId``.
- UnlockConnector: ``UnknownConnector`` for an unknown EVSE or a connector other than 1,
  ``OngoingAuthorizedTransaction`` while the EVSE has a transaction (it is not stopped), else ``Unlocked`` (or
  ``UnlockFailed`` when :attr:`Station201.unlock_fails` is set). Unlocking changes no state (the cable stays
  plugged until the operator unplugs it).
- Reset: an EVSE reset (``evseId`` given) is not supported and ``Rejected``. ``Immediate`` ends every transaction
  (``ResetCommand`` / ``ImmediateReset``), lets the queued messages go out (bounded by ``host.call_timeout``) and
  reboots: the session closes and reconnects without back-off, with BootNotification reason ``RemoteReset``.
  ``OnIdle`` does the same at once when no transaction runs, else answers ``Scheduled`` and resets when the last
  transaction has ended; new transactions may still start meanwhile. An ``Immediate`` Reset overrides a scheduled
  one. Cable, fault and availability states survive the reboot; tokens waiting for a cable do not.
- ChangeAvailability: no ``evse`` or EVSE id 0 addresses the whole station, i.e. every EVSE (there is no separate
  station state); an unknown EVSE or a connector other than 1 is ``Rejected``. Going ``Inoperative`` while an
  addressed EVSE has a transaction is ``Scheduled`` for that EVSE and takes effect when its transaction ends;
  every other change takes effect at once (``Accepted``, also when the state does not change). ``Operative``
  cancels a scheduled change. Availability survives a Reset. A token waiting on an EVSE that becomes
  inoperative is dropped.
- TriggerMessage: BootNotification is ``Accepted`` only while the registration is not ``Accepted`` (it wakes the
  boot loop; reason ``Triggered``), else ``Rejected``. Heartbeat, StatusNotification, MeterValues and
  TransactionEvent are ``Rejected`` while the boot is not accepted (only BootNotification is sent then).
  StatusNotification and MeterValues (context ``Trigger``, :data:`TX_UPDATED_MEASURANDS`) are sent for the
  addressed EVSE, or every EVSE without ``evse``; TransactionEvent sends an ``Updated`` (``triggerReason``
  ``Trigger``, the same readings) for the transaction of the addressed EVSE, or of every EVSE, and is
  ``Rejected`` when there is none. An unknown EVSE or a connector other than 1 is ``Rejected``. The other
  ``requestedMessage`` values (LogStatusNotification, FirmwareStatusNotification, the certificate messages, ...)
  answer ``NotImplemented``.
- A ``Rejected`` (or ``UnknownConnector``) answer carries ``statusInfo.reasonCode`` with a short simulator code
  (:data:`REASON_UNKNOWN_EVSE` ...); the codes are informative only.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Sequence, Tuple

from ...charging import power
from ...charging.power import PowerState
from ...rpc import CallError, wait_event
from .. import base as d
from ..base import AuthResult as A
from ..base import DomainStatus as S
from ..station import StationHost, VersionStation
from . import models as m
from .adapter import LOCAL_TOKEN_TYPE, Adapter201, connector_status, id_token, meter_value
from .enums import (BootReasonEnumType, ChangeAvailabilityStatusEnumType, ChargingProfilePurposeEnumType,
                    ChargingStateEnumType, ConnectorStatusEnumType, IdTokenEnumType, MessageTriggerEnumType,
                    OperationalStatusEnumType, ReadingContextEnumType, ReasonEnumType,
                    RequestStartStopStatusEnumType, ResetEnumType, ResetStatusEnumType, TransactionEventEnumType,
                    TriggerMessageStatusEnumType, TriggerReasonEnumType, UnlockStatusEnumType)

log = logging.getLogger(__name__)

ENERGY = "Energy.Active.Import.Register"
POWER = "Power.Active.Import"
CURRENT = "Current.Import"
VOLTAGE = "Voltage"
SOC = "SoC"
POWER_DECIMALS = 1
CURRENT_DECIMALS = 3
SOC_DECIMALS = 1
LOCATION = "Outlet"
SOC_LOCATION = "EV"

#: Connector id of every EVSE (one connector per EVSE, plan §11.1 Q11).
CONNECTOR_ID = 1
#: Heartbeat interval until a BootNotification answer sets one.
DEFAULT_HEARTBEAT_S = 300
DEFAULT_TX_UPDATED_INTERVAL_S = 60
TX_UPDATED_MEASURANDS: Tuple[str, ...] = (ENERGY, POWER)
TX_ENDED_MEASURANDS: Tuple[str, ...] = (ENERGY,)
#: Retry policy of a TransactionEvent the CSMS failed to process (OCPPCommCtrlr.MessageAttempts* in M4).
TX_MESSAGE_ATTEMPTS = 3
TX_MESSAGE_RETRY_INTERVAL_S = 10
#: Maximum number of queued periodic TransactionEvent Updated; the oldest is dropped first.
MAX_TX_QUEUE = 1000

#: Poll step while a Reset waits for the outbox to drain.
RESET_DRAIN_POLL_S = 0.05
#: ``statusInfo.reasonCode`` of a refused remote-control request (see the module docstring).
REASON_UNKNOWN_EVSE = "UnknownEvse"
REASON_OCCUPIED = "Occupied"
REASON_FAULTED = "Faulted"
REASON_UNAVAILABLE = "Unavailable"
REASON_INVALID_PROFILE = "InvalidProfile"
REASON_UNKNOWN_TX = "UnknownTransaction"
REASON_NOT_SUPPORTED = "NotSupported"
REASON_NOT_REGISTERED = "NotRegistered"
REASON_REGISTERED = "AlreadyRegistered"
REASON_NO_TX = "NoTransaction"

T = TriggerReasonEnumType
MT = MessageTriggerEnumType
#: TriggerMessage values this station serves; every other one is NotImplemented.
TRIGGERABLE = frozenset({MT.boot_notification, MT.heartbeat, MT.status_notification, MT.meter_values,
                         MT.transaction_event})
_SUSPENSION = {"SuspendedEV": ChargingStateEnumType.suspended_ev, "SuspendedEVSE": ChargingStateEnumType.suspended_evse}
#: Fault-type values of ReasonEnumType an operator may set (see the module docstring).
FAULT_CODES: Tuple[str, ...] = tuple(r.value for r in (
    ReasonEnumType.emergency_stop, ReasonEnumType.ground_fault, ReasonEnumType.overcurrent_fault,
    ReasonEnumType.power_loss, ReasonEnumType.power_quality, ReasonEnumType.other))


@dataclass
class _Tx:
    tx_id: str
    id_token: str
    meter_start_wh: int
    started_at: datetime
    token_type: IdTokenEnumType = LOCAL_TOKEN_TYPE
    remote_start_id: Optional[int] = None
    next_seq: int = 0
    charging_state: Optional[ChargingStateEnumType] = None  # last one reported

    def seq(self) -> int:
        n, self.next_seq = self.next_seq, self.next_seq + 1
        return n


@dataclass
class _Evse:
    id: int
    status: S = S.AVAILABLE
    #: connectorStatus last reported (sent or queued) in this session; ``None`` = not yet.
    reported: Optional[ConnectorStatusEnumType] = None
    plugged: bool = False
    operative: bool = True
    fault: Optional[str] = None
    finishing: bool = False
    pending_tag: Optional[str] = None  # authorized, waiting for the cable
    pending_type: IdTokenEnumType = LOCAL_TOKEN_TYPE
    pending_remote_id: Optional[int] = None  # remoteStartId of a RequestStartTransaction token
    pending_operative: Optional[bool] = None  # ChangeAvailability "Scheduled"
    tx: Optional[_Tx] = None
    meter_wh: float = 0.0
    pw: Optional[PowerState] = None
    last_mono: Optional[float] = None
    meter_task: Optional[asyncio.Task] = None


class _Delivery:
    DONE, RETRY, OFFLINE = "done", "retry", "offline"


@dataclass(eq=False)  # identity semantics: items are removed by identity
class _Out:
    what: str
    send: Callable[[], Awaitable[Any]]
    #: TransactionEvent (kept across sessions, retried) vs StatusNotification.
    transactional: bool = False
    #: Periodic Updated: may be dropped by the queue bound.
    droppable: bool = False
    evse: Optional[_Evse] = None
    tx: Optional[_Tx] = None
    req: Optional[m.TransactionEventRequest] = None
    failures: int = 0


class Station201(VersionStation):
    fault_codes = FAULT_CODES

    def __init__(self, host: StationHost, *, strict: bool = False) -> None:
        super().__init__(host, strict=strict)
        cfg = self.cfg = host.cfg
        self.ev, self.supply = host.ev, host.supply
        self._adapter = Adapter201(strict=strict)
        self.evses: Dict[int, _Evse] = {i: _Evse(i) for i in range(1, cfg.connectors + 1)}
        interval = cfg.meter_sample_interval_s
        self.tx_updated_interval_s = DEFAULT_TX_UPDATED_INTERVAL_S if interval is None else interval
        self.heartbeat_interval_s = DEFAULT_HEARTBEAT_S
        self._hb_wake = asyncio.Event()
        #: (vendorId, messageId or None) -> answer for inbound DataTransfer; default UnknownVendorId.
        self.data_transfer_rules: Dict[Tuple[str, Optional[str]], d.DataTransferResult] = {}
        self._outq: Deque[_Out] = deque()
        self._outq_wake = asyncio.Event()
        self._outq_busy: Optional[_Out] = None
        #: UnlockConnector answers UnlockFailed (an operator / test switch; no lock is modelled).
        self.unlock_fails = False
        self._reset_scheduled = False  # an OnIdle Reset waits for the transactions to end
        self._resetting = False
        self._remote: Dict[type, Callable[[Any], m.OcppModel]] = {
            m.RequestStartTransactionRequest: self._request_start,
            m.RequestStopTransactionRequest: self._request_stop,
            m.UnlockConnectorRequest: self._unlock,
            m.ResetRequest: self._reset,
            m.ChangeAvailabilityRequest: self._change_availability,
            m.TriggerMessageRequest: self._trigger,
        }

    # ================================================================== session (called by the shell)

    @property
    def adapter(self) -> Adapter201:
        return self._adapter

    @property
    def connector_count(self) -> int:
        return len(self.evses)

    async def on_online(self) -> None:
        # Bring every state up to date first (a transaction may have ended while offline), then report.
        # ``reported`` is set for every EVSE before the first await, so a refresh meanwhile queues no duplicate.
        for e in self.evses.values():
            e.status = self._target(e)
            e.reported = connector_status(e.status)
        for e in self.evses.values():
            await self.host.safe(self._adapter.send_connector_status(self._status_event(e)), "StatusNotification")
        self._adapter.boot_reason = BootReasonEnumType.power_up  # a RemoteReset boot has been accepted
        self.host.spawn(self._outbox_worker())
        self.host.start_ticks()
        for e in self.evses.values():
            if e.tx is not None:
                self._start_meter(e)
        self._maybe_idle_reset()  # an OnIdle Reset whose last transaction ended while offline

    def on_session_ended(self) -> None:
        for e in self.evses.values():
            e.meter_task = None  # cancelled with the other session tasks
            e.reported = None
        for item in [x for x in self._outq if not x.transactional]:
            self._outq.remove(item)

    async def wait_heartbeat(self) -> bool:
        interval = self.heartbeat_interval_s
        return await wait_event(self._hb_wake, interval if interval > 0 else None)

    def set_heartbeat_interval(self, seconds: int) -> None:
        self.heartbeat_interval_s = seconds
        self._hb_wake.set()  # broadcast to the waiting loop, then re-arm
        self._hb_wake = asyncio.Event()

    def snapshot(self) -> Dict[str, Any]:
        def evse(e: _Evse) -> Dict[str, Any]:
            tx = None
            if e.tx is not None:
                tx = {"tx_id": e.tx.tx_id, "id_tag": e.tx.id_token, "meter_start_wh": e.tx.meter_start_wh,
                      "started_at": e.tx.started_at.isoformat(), "seq_no": e.tx.next_seq,
                      "charging_state": e.tx.charging_state.value if e.tx.charging_state else None}
            return {"id": e.id, "evse_id": e.id, "connector_id": CONNECTOR_ID, "status": e.status.value,
                    "connector_status": connector_status(e.status).value, "error_code": e.fault,
                    "plugged": e.plugged, "operative": e.operative, "pending_operative": e.pending_operative,
                    "pending_tag": e.pending_tag, "meter_wh": round(e.meter_wh, 3),
                    "power_w": round(e.pw.power_w, POWER_DECIMALS) if e.pw is not None else 0.0,
                    "soc_pct": round(e.pw.soc_pct, SOC_DECIMALS) if e.pw is not None else None,
                    "transaction": tx}

        return {
            "tx_queue": sum(1 for x in self._outq if x.transactional),
            # 2.0.1 has no station-level status; the 1.6-shaped key keeps the Web UI working.
            "station": {"status": S.AVAILABLE.value, "operative": all(e.operative for e in self.evses.values())},
            "connectors": [evse(e) for e in self.evses.values()],
        }

    def configuration(self) -> List[d.ConfigEntry]:
        return []  # the Device Model arrives with M4

    # ================================================================== outbox

    def _push(self, item: _Out) -> None:
        """Queue ``item`` (synchronous: never lost to a cancelled caller)."""
        if item.droppable:
            periodic = [x for x in self._outq if x.droppable and x is not self._outq_busy]
            if len(periodic) >= MAX_TX_QUEUE:
                self._outq.remove(periodic[0])
                log.warning("%s: outbox full (%d periodic TransactionEvents), oldest dropped", self.cfg.id,
                            MAX_TX_QUEUE)
        self._outq.append(item)
        self._outq_wake.set()

    async def _outbox_worker(self) -> None:
        """Session task: send queued messages strictly in order; stops (head kept) when the connection is lost."""
        while True:
            while self._outq:
                item = self._outq[0]
                self._outq_busy = item
                try:
                    if item.failures:
                        await asyncio.sleep(TX_MESSAGE_RETRY_INTERVAL_S * item.failures)
                    delivery, value = await self._deliver(item)
                finally:
                    self._outq_busy = None
                if delivery == _Delivery.OFFLINE:
                    self.host.abort_session()  # a stalled outbox never coexists with a live session
                    return
                if delivery == _Delivery.DONE:
                    if item in self._outq:
                        self._outq.remove(item)
                    if value is not None and not self._apply_answer(item, value):
                        return
            self._outq_wake.clear()
            await self._outq_wake.wait()

    async def _deliver(self, item: _Out) -> Tuple[str, Any]:
        try:
            return _Delivery.DONE, await item.send()
        except asyncio.CancelledError:
            raise
        except ConnectionError as e:
            log.info("%s: %s kept queued (%r)", self.cfg.id, item.what, e)
            return _Delivery.OFFLINE, None
        except (CallError, TimeoutError, ValueError) as e:  # the CSMS failed to process it
            if not item.transactional:
                log.warning("%s: %s failed: %r", self.cfg.id, item.what, e)
                return _Delivery.DONE, None
            item.failures += 1
            if item.failures >= TX_MESSAGE_ATTEMPTS:
                log.warning("%s: %s discarded after %d failed attempts: %r", self.cfg.id, item.what,
                            item.failures, e)
                return _Delivery.DONE, None
            log.warning("%s: %s failed (%d/%d): %r", self.cfg.id, item.what, item.failures,
                        TX_MESSAGE_ATTEMPTS, e)
            return _Delivery.RETRY, None
        except Exception:  # noqa: BLE001 - a local bug, not the CSMS: drop the item, keep the worker alive
            log.exception("%s: %s dropped (internal error)", self.cfg.id, item.what)
            return _Delivery.DONE, None

    def _apply_answer(self, item: _Out, res: Any) -> bool:
        """Apply a TransactionEvent answer; on a local bug end the session and return False."""
        try:
            if isinstance(res, m.TransactionEventResponse):
                self._on_tx_answer(item, res)
            return True
        except Exception as e:  # noqa: BLE001 - fail loudly, restart the session
            log.exception("%s: applying the %s answer failed", self.cfg.id, item.what)
            self.host.abort_session(repr(e))
            return False

    def _on_tx_answer(self, item: _Out, res: m.TransactionEventResponse) -> None:
        """A CSMS that does not accept the token of a running transaction ends it (StopTxOnInvalidId)."""
        e, tx, req = item.evse, item.tx, item.req
        if res.id_token_info is None or e is None or tx is None or req is None or req.id_token is None:
            return
        status = res.id_token_info.status.value
        if status != A.ACCEPTED.value and e.tx is tx:
            log.info("%s: EVSE %d token %s answered %s: transaction %s ends", self.cfg.id, e.id, tx.id_token,
                     status, tx.tx_id)
            self._end_tx(e, T.deauthorized, ReasonEnumType.de_authorized)

    # ================================================================== status

    def _target(self, e: _Evse) -> S:
        if e.fault:
            return S.FAULTED
        if not e.operative:
            return S.UNAVAILABLE
        if e.tx is not None:
            return S(e.pw.suspension) if e.pw is not None and e.pw.suspension else S.CHARGING
        if e.plugged:
            return S.FINISHING if e.finishing else S.PREPARING
        return S.AVAILABLE

    def _status_event(self, e: _Evse) -> d.ConnectorStatus:
        return d.ConnectorStatus(e.id, CONNECTOR_ID, e.status, timestamp=self.host.clock())

    def _refresh(self, e: _Evse) -> None:
        """Move ``e`` to its target state; when online and the connectorStatus changed, queue the
        StatusNotification (never waits)."""
        e.status = self._target(e)
        wire = connector_status(e.status)
        if self.host.online and wire != e.reported:
            e.reported = wire
            ev = self._status_event(e)  # captured now: the state may change before it is sent
            self._push(_Out("StatusNotification", lambda: self._adapter.send_connector_status(ev)))

    # ================================================================== readings / power model

    def _samples(self, e: _Evse, measurands: Sequence[str]) -> List[d.Sample]:
        r = power.meter_reading(e.pw, self.ev, self.supply) if e.pw is not None else None
        watts = r.power_active_import_w if r else 0.0
        amps = r.current_import_a if r else 0.0
        out: List[d.Sample] = []
        for name in measurands:
            if name == ENERGY:
                out.append(d.Sample(name, round(e.meter_wh), "Wh", location=LOCATION))
            elif name == POWER:
                out.append(d.Sample(name, round(watts, POWER_DECIMALS), "W", location=LOCATION))
            elif name == CURRENT:
                out.append(d.Sample(name, round(amps, CURRENT_DECIMALS), "A", location=LOCATION))
            elif name == VOLTAGE:
                out.append(d.Sample(name, self.supply.voltage_v, "V", location=LOCATION))
            elif name == SOC and r is not None:
                out.append(d.Sample(name, round(r.soc_pct, SOC_DECIMALS), "Percent", location=SOC_LOCATION))
        return out

    def _advance(self, e: _Evse) -> None:
        """Step the power model of a charging EVSE up to now and restart the interval."""
        now = self.host.monotonic()
        if e.pw is not None and not self.host.paused:  # paused: frozen, the interval just restarts
            dt = max(0.0, now - e.last_mono) if e.last_mono is not None else 0.0
            e.pw = self.host.step_power(e.pw, dt, None)  # no smart charging before M5
            e.meter_wh = e.pw.energy_wh
        e.last_mono = now

    def settle(self) -> None:
        for e in self.evses.values():
            self._advance(e)

    def tick(self) -> None:
        for e in self.evses.values():
            if e.pw is not None:
                self._advance(e)
                self._refresh(e)
                self._report_charging_state(e)

    @staticmethod
    def _charging_state(e: _Evse) -> ChargingStateEnumType:
        if e.tx is not None and e.pw is not None:
            return _SUSPENSION.get(e.pw.suspension or "", ChargingStateEnumType.charging)
        return ChargingStateEnumType.ev_connected if e.plugged else ChargingStateEnumType.idle

    def _report_charging_state(self, e: _Evse) -> None:
        tx = e.tx
        if tx is None:
            return
        state = self._charging_state(e)
        if state != tx.charging_state:
            tx.charging_state = state
            self._queue_event(e, tx, TransactionEventEnumType.updated, T.charging_state_changed,
                              m.TransactionType(transaction_id=tx.tx_id, charging_state=state))

    # ================================================================== transaction events

    def _queue_event(self, e: _Evse, tx: _Tx, event: TransactionEventEnumType, trigger: TriggerReasonEnumType,
                     info: m.TransactionType, *, samples: Sequence[d.Sample] = (),
                     context: Optional[ReadingContextEnumType] = None, token: Optional[str] = None,
                     token_type: IdTokenEnumType = LOCAL_TOKEN_TYPE, droppable: bool = False) -> None:
        now = self.host.clock()
        req = m.TransactionEventRequest(
            event_type=event, timestamp=now, trigger_reason=trigger, seq_no=tx.seq(),
            offline=True if not self.host.connected else None, transaction_info=info,
            evse=m.EVSEType(id=e.id, connector_id=CONNECTOR_ID),
            id_token=id_token(token, token_type) if token is not None else None,
            meter_value=[meter_value(now, samples, context.value)] if samples and context else None)
        self._push(_Out(f"TransactionEvent {event.value}", lambda: self._adapter.send_transaction_event(req),
                        transactional=True, droppable=droppable, evse=e, tx=tx, req=req))

    def _start_tx(self, e: _Evse, tag: str, trigger: TriggerReasonEnumType, *,
                  token_type: IdTokenEnumType = LOCAL_TOKEN_TYPE, remote_start_id: Optional[int] = None) -> None:
        """Start a transaction on a plugged, operative EVSE for an authorized ``tag`` (synchronous)."""
        self._clear_pending(e)
        e.finishing = False
        tx = e.tx = _Tx(str(uuid.uuid4()), tag, int(round(e.meter_wh)), self.host.clock(), token_type=token_type,
                        remote_start_id=remote_start_id)
        e.pw = PowerState(soc_pct=self.cfg.session.initial_soc, energy_wh=e.meter_wh)  # a fresh EV session
        e.last_mono = None
        self._advance(e)  # zero-length step: initial power and suspension
        tx.charging_state = self._charging_state(e)
        self._queue_event(e, tx, TransactionEventEnumType.started, trigger,
                          m.TransactionType(transaction_id=tx.tx_id, charging_state=tx.charging_state,
                                            remote_start_id=remote_start_id),
                          samples=self._samples(e, (ENERGY,)), context=ReadingContextEnumType.transaction_begin,
                          token=tag, token_type=token_type)
        self._refresh(e)
        if self.host.online:
            self._start_meter(e)

    def _end_tx(self, e: _Evse, trigger: TriggerReasonEnumType, reason: ReasonEnumType, *,
                token: Optional[str] = None) -> None:
        """End the running transaction of ``e``; never waits for the CSMS."""
        tx = e.tx
        if tx is None:
            return
        self._advance(e)
        samples = self._samples(e, TX_ENDED_MEASURANDS)
        if e.meter_task is not None and e.meter_task is not asyncio.current_task():
            e.meter_task.cancel()
        e.meter_task = None
        e.tx, e.pw, e.last_mono, e.finishing = None, None, None, e.plugged
        self._queue_event(e, tx, TransactionEventEnumType.ended, trigger,
                          m.TransactionType(transaction_id=tx.tx_id, charging_state=self._charging_state(e),
                                            stopped_reason=reason),
                          samples=samples, context=ReadingContextEnumType.transaction_end, token=token)
        if e.pending_operative is not None:  # a Scheduled ChangeAvailability takes effect now
            self._set_operative(e, e.pending_operative)
        self._refresh(e)
        self._maybe_idle_reset()

    def _start_meter(self, e: _Evse) -> None:
        if self.tx_updated_interval_s > 0 and (e.meter_task is None or e.meter_task.done()):
            e.meter_task = self.host.spawn(self._meter_loop(e))

    async def _meter_loop(self, e: _Evse) -> None:
        while e.tx is not None:
            await asyncio.sleep(self.tx_updated_interval_s)
            tx = e.tx
            if tx is None:
                return
            self._advance(e)
            self._queue_event(e, tx, TransactionEventEnumType.updated, T.meter_value_periodic,
                              m.TransactionType(transaction_id=tx.tx_id),
                              samples=self._samples(e, TX_UPDATED_MEASURANDS),
                              context=ReadingContextEnumType.sample_periodic, droppable=True)

    # ================================================================== authorization

    async def _authorize(self, tag: str) -> A:
        mode = self.cfg.auth_mode
        if mode == "accept_all":
            return A.ACCEPTED
        local = tag in self.cfg.id_tags
        if mode == "local_only":
            return A.ACCEPTED if local else A.INVALID
        if mode == "local_then_csms" and local:
            return A.ACCEPTED
        if not self.host.online:
            return A.OFFLINE  # TODO(spec): offline authorization (LocalAuthListCtrlr / AuthCacheCtrlr) is M6
        info: Optional[d.IdTokenInfo] = await self.host.safe(
            self._adapter.send_authorize(d.AuthorizeRequest(tag)), "Authorize")
        return info.status if info is not None else A.ERROR

    # ================================================================== CSMS commands

    async def handle(self, cmd: d.Command) -> Any:
        if isinstance(cmd, d.DataTransfer):
            rule = (self.data_transfer_rules.get((cmd.vendor_id, cmd.message_id))
                    or self.data_transfer_rules.get((cmd.vendor_id, None)))
            return rule or d.DataTransferResult(d.DataTransferStatus.UNKNOWN_VENDOR_ID)
        remote = self._remote.get(type(cmd))
        if remote is not None:
            return remote(cmd)
        return d.CommandStatus.NOT_IMPLEMENTED

    # ================================================================== remote control (M3)

    @staticmethod
    def _info(code: Optional[str]) -> Optional[m.StatusInfoType]:
        return m.StatusInfoType(reason_code=code) if code else None

    def _addressed(self, evse: Optional[m.EVSEType]) -> Optional[List[_Evse]]:
        """EVSEs an optional ``evse`` field addresses (none / id 0 = all); ``None`` when it is unknown."""
        if evse is None or evse.id == 0:
            return list(self.evses.values()) if evse is None or evse.connector_id is None else None
        e = self.evses.get(evse.id)
        if e is None or evse.connector_id not in (None, CONNECTOR_ID):
            return None
        return [e]

    @staticmethod
    def _start_blocker(e: _Evse) -> Optional[str]:
        """Why no transaction can be started on ``e`` now (``None`` = it can)."""
        if e.fault:
            return REASON_FAULTED
        if not e.operative:
            return REASON_UNAVAILABLE
        if e.tx is not None:
            return REASON_OCCUPIED
        return None

    @staticmethod
    def _clear_pending(e: _Evse) -> None:
        e.pending_tag, e.pending_type, e.pending_remote_id = None, LOCAL_TOKEN_TYPE, None

    def _set_operative(self, e: _Evse, operative: bool) -> None:
        e.operative, e.pending_operative = operative, None
        if not operative:
            self._clear_pending(e)

    def _request_start(self, req: m.RequestStartTransactionRequest) -> m.RequestStartTransactionResponse:
        def rejected(code: str) -> m.RequestStartTransactionResponse:
            return m.RequestStartTransactionResponse(status=RequestStartStopStatusEnumType.rejected,
                                                     status_info=self._info(code))

        profile = req.charging_profile
        if profile is not None and (profile.charging_profile_purpose is not ChargingProfilePurposeEnumType.tx_profile
                                    or profile.transaction_id is not None):
            return rejected(REASON_INVALID_PROFILE)
        if req.evse_id is not None:
            e = self.evses.get(req.evse_id)
            if e is None:
                return rejected(REASON_UNKNOWN_EVSE)
            blocker = self._start_blocker(e)
            if blocker:
                return rejected(blocker)
        else:
            free = [x for x in self.evses.values() if self._start_blocker(x) is None]
            if not free:
                return rejected(REASON_OCCUPIED)
            e = next((x for x in free if x.plugged), free[0])
        # Claimed now, so that the EVSE is not handed out twice; the transaction starts after the answer.
        e.pending_tag, e.pending_type = req.id_token.id_token, req.id_token.type
        e.pending_remote_id = req.remote_start_id
        self.host.after_reply(functools.partial(self._remote_start_go, e, req.remote_start_id))
        return m.RequestStartTransactionResponse(status=RequestStartStopStatusEnumType.accepted)

    def _remote_start_go(self, e: _Evse, remote_start_id: int) -> None:
        if e.pending_remote_id != remote_start_id or e.pending_tag is None or e.tx is not None:
            return  # replaced, started or dropped meanwhile
        if self._start_blocker(e) is not None:
            self._clear_pending(e)
            return
        if e.plugged:
            self._start_tx(e, e.pending_tag, T.remote_start, token_type=e.pending_type,
                           remote_start_id=e.pending_remote_id)
        else:
            self._refresh(e)  # waits for the cable

    def _request_stop(self, req: m.RequestStopTransactionRequest) -> m.RequestStopTransactionResponse:
        for e in self.evses.values():
            if e.tx is not None and e.tx.tx_id == req.transaction_id:
                self.host.after_reply(functools.partial(self._remote_stop, e, e.tx))
                return m.RequestStopTransactionResponse(status=RequestStartStopStatusEnumType.accepted)
        return m.RequestStopTransactionResponse(status=RequestStartStopStatusEnumType.rejected,
                                                status_info=self._info(REASON_UNKNOWN_TX))

    def _remote_stop(self, e: _Evse, tx: _Tx) -> None:
        if e.tx is tx:
            self._end_tx(e, T.remote_stop, ReasonEnumType.remote)

    def _unlock(self, req: m.UnlockConnectorRequest) -> m.UnlockConnectorResponse:
        e = self.evses.get(req.evse_id)
        if e is None or req.connector_id != CONNECTOR_ID:
            return m.UnlockConnectorResponse(status=UnlockStatusEnumType.unknown_connector,
                                             status_info=self._info(REASON_UNKNOWN_EVSE))
        if e.tx is not None:
            return m.UnlockConnectorResponse(status=UnlockStatusEnumType.ongoing_authorized_transaction)
        if self.unlock_fails:
            return m.UnlockConnectorResponse(status=UnlockStatusEnumType.unlock_failed)
        return m.UnlockConnectorResponse(status=UnlockStatusEnumType.unlocked)

    def _reset(self, req: m.ResetRequest) -> m.ResetResponse:
        if req.evse_id is not None:
            return m.ResetResponse(status=ResetStatusEnumType.rejected, status_info=self._info(REASON_NOT_SUPPORTED))
        if req.type is ResetEnumType.on_idle and any(e.tx is not None for e in self.evses.values()):
            self._reset_scheduled = True
            return m.ResetResponse(status=ResetStatusEnumType.scheduled)
        self._reset_scheduled = False
        self.host.after_reply(self._do_reset)
        return m.ResetResponse(status=ResetStatusEnumType.accepted)

    def _maybe_idle_reset(self) -> None:
        """Run a scheduled OnIdle Reset once no transaction is left (when connected; else at the next session)."""
        if not self._reset_scheduled or any(e.tx is not None for e in self.evses.values()):
            return
        self._reset_scheduled = False
        if self.host.spawn(self._do_reset()) is None:
            self._reset_scheduled = True  # not connected: the next session runs it

    async def _do_reset(self) -> None:
        if self._resetting:
            return
        self._resetting = True
        try:
            self._reset_scheduled = False
            for e in self.evses.values():
                self._end_tx(e, T.reset_command, ReasonEnumType.immediate_reset)
                self._clear_pending(e)
            self._adapter.boot_reason = BootReasonEnumType.remote_reset
            # Let the queued messages go out first; bounded, a silent CSMS must not block the Reset (anything
            # left stays queued for the next session).
            try:
                async with asyncio.timeout(self.host.call_timeout):
                    while self._outq and self.host.connected:
                        await asyncio.sleep(RESET_DRAIN_POLL_S)
            except TimeoutError:
                log.info("%s: Reset closes with %d queued message(s)", self.cfg.id, len(self._outq))
        finally:
            self._resetting = False
            self.host.end_session_for_reset()

    def _change_availability(self, req: m.ChangeAvailabilityRequest) -> m.ChangeAvailabilityResponse:
        targets = self._addressed(req.evse)
        if targets is None:
            return m.ChangeAvailabilityResponse(status=ChangeAvailabilityStatusEnumType.rejected,
                                                status_info=self._info(REASON_UNKNOWN_EVSE))
        operative = req.operational_status is OperationalStatusEnumType.operative
        scheduled = False
        for e in targets:
            if not operative and e.operative and e.tx is not None:
                e.pending_operative, scheduled = False, True
            else:
                self._set_operative(e, operative)
        self.host.after_reply(functools.partial(self._refresh_all, targets))
        return m.ChangeAvailabilityResponse(status=ChangeAvailabilityStatusEnumType.scheduled if scheduled
                                            else ChangeAvailabilityStatusEnumType.accepted)

    def _refresh_all(self, targets: Sequence[_Evse]) -> None:
        for e in targets:
            self._refresh(e)

    def _trigger(self, req: m.TriggerMessageRequest) -> m.TriggerMessageResponse:
        def answer(status: TriggerMessageStatusEnumType, code: Optional[str] = None) -> m.TriggerMessageResponse:
            return m.TriggerMessageResponse(status=status, status_info=self._info(code))

        what = req.requested_message
        if what not in TRIGGERABLE:
            return answer(TriggerMessageStatusEnumType.not_implemented)
        targets = self._addressed(req.evse)
        if targets is None:
            return answer(TriggerMessageStatusEnumType.rejected, REASON_UNKNOWN_EVSE)
        if what is MT.boot_notification:
            if self.host.registration == d.BootStatus.ACCEPTED.value:
                return answer(TriggerMessageStatusEnumType.rejected, REASON_REGISTERED)
            self._adapter.trigger_boot_reason(BootReasonEnumType.triggered)
            self.host.after_reply(self.host.kick_boot)
            return answer(TriggerMessageStatusEnumType.accepted)
        if not self.host.online:
            return answer(TriggerMessageStatusEnumType.rejected, REASON_NOT_REGISTERED)
        if what is MT.heartbeat:
            self.host.after_reply(self.host.heartbeat)
        elif what is MT.status_notification:
            self.host.after_reply(functools.partial(self._trigger_statuses, targets))
        elif what is MT.meter_values:
            self.host.after_reply(functools.partial(self._trigger_meter_values, targets))
        else:  # TransactionEvent
            with_tx = [e for e in targets if e.tx is not None]
            if not with_tx:
                return answer(TriggerMessageStatusEnumType.rejected, REASON_NO_TX)
            self.host.after_reply(functools.partial(self._trigger_tx_events, with_tx))
        return answer(TriggerMessageStatusEnumType.accepted)

    def _trigger_statuses(self, targets: Sequence[_Evse]) -> None:
        """Queue the current connectorStatus of ``targets`` (also when unchanged)."""
        for e in targets:
            e.status = self._target(e)
            e.reported = connector_status(e.status)
            ev = self._status_event(e)
            self._push(_Out("StatusNotification", functools.partial(self._adapter.send_connector_status, ev)))

    async def _trigger_meter_values(self, targets: Sequence[_Evse]) -> None:
        for e in targets:
            self._advance(e)
            ev = d.MeterSample(e.id, self.host.clock(), self._samples(e, TX_UPDATED_MEASURANDS),
                               ReadingContextEnumType.trigger.value)
            await self.host.safe(self._adapter.send_meter_sample(ev), "MeterValues")

    def _trigger_tx_events(self, targets: Sequence[_Evse]) -> None:
        for e in targets:
            tx = e.tx
            if tx is None:
                continue  # ended meanwhile
            self._advance(e)
            tx.charging_state = self._charging_state(e)
            self._queue_event(e, tx, TransactionEventEnumType.updated, T.trigger,
                              m.TransactionType(transaction_id=tx.tx_id, charging_state=tx.charging_state),
                              samples=self._samples(e, TX_UPDATED_MEASURANDS), context=ReadingContextEnumType.trigger)

    # ================================================================== operator API (engine / Web API)

    def _evse(self, connector: int) -> _Evse:
        try:
            return self.evses[connector]
        except KeyError:
            raise ValueError(f"no EVSE {connector} (1..{self.connector_count})") from None

    async def plug(self, connector: int) -> None:
        e = self._evse(connector)
        if e.plugged:
            return
        e.plugged = True
        if e.pending_tag and e.operative and not e.fault:
            self._start_tx(e, e.pending_tag, T.cable_plugged_in, token_type=e.pending_type,
                           remote_start_id=e.pending_remote_id)
        else:
            self._refresh(e)

    async def unplug(self, connector: int) -> None:
        """Never waits for the CSMS."""
        e = self._evse(connector)
        if not e.plugged:
            return
        e.plugged, e.finishing = False, False
        self._clear_pending(e)  # a waiting token does not outlive the cable
        if e.tx is not None:
            self._end_tx(e, T.ev_communication_lost, ReasonEnumType.ev_disconnected)
        else:
            self._refresh(e)

    async def swipe(self, connector: int, id_tag: str) -> A:
        """Present an id token. ``Accepted`` = a transaction started (or the token waits for the cable),
        ``Stopped`` = it ended its own transaction; the authorization status when refused; ``Busy`` /
        ``Unavailable`` / ``Offline`` / ``Error`` when no transaction could be started."""
        e = self._evse(connector)
        if e.tx is not None:
            if e.tx.id_token == id_tag:
                self._end_tx(e, T.stop_authorized, ReasonEnumType.local, token=id_tag)
                return A.STOPPED
            return A.BUSY
        if e.fault or not e.operative:
            return A.UNAVAILABLE
        status = await self._authorize(id_tag)
        if status is not A.ACCEPTED:
            return status
        # Re-check: the await may have let another start / a fault in.
        if e.tx is not None:
            return A.BUSY
        if e.fault or not e.operative:
            return A.UNAVAILABLE
        if not e.plugged:
            self._clear_pending(e)
            e.pending_tag = id_tag
            self._refresh(e)
            return A.ACCEPTED
        self._start_tx(e, id_tag, T.authorized)
        return A.ACCEPTED

    async def stop_tx(self, connector: int) -> bool:
        """Never waits for the CSMS: the TransactionEvent Ended is queued before this returns."""
        e = self._evse(connector)
        if e.tx is None:
            return False
        self._end_tx(e, T.stop_authorized, ReasonEnumType.local)
        return True

    async def fault(self, connector: int, error_code: str) -> None:
        """``error_code`` is one of :attr:`fault_codes` (a ``ReasonEnumType`` value)."""
        e = self._evse(connector)
        if error_code not in self.fault_codes:
            raise ValueError(f"fault code {error_code!r} is not one of {', '.join(self.fault_codes)}")
        e.fault = error_code
        if e.tx is not None:
            self._end_tx(e, T.abnormal_condition, ReasonEnumType(error_code))
        self._refresh(e)

    async def clear_fault(self, connector: int) -> bool:
        e = self._evse(connector)
        if e.fault is None:
            return False
        e.fault = None
        self._refresh(e)
        return True

    def has_transaction(self, connector: int) -> bool:
        return self._evse(connector).tx is not None

    def faulted(self, connector: int) -> Optional[str]:
        return self._evse(connector).fault

    def installed_profiles(self, connector: Optional[int] = None) -> Tuple[Any, ...]:
        if connector:
            self._evse(connector)
        return ()  # smart charging is M5

    def composite(self, connector: int, duration: int, unit: Optional[str] = None) -> d.GetCompositeResult:
        if unit not in (None, "W", "A"):
            raise ValueError(f"unit {unit!r}: W or A")
        return d.GetCompositeResult(d.CommandStatus.REJECTED)  # smart charging is M5

    async def send_meter_values(self, connector: int, context: str = ReadingContextEnumType.other.value) -> None:
        """Send a MeterValues of ``connector`` now (outside the TransactionEvent flow; M3 TriggerMessage uses it)."""
        e = self._evse(connector)
        self._advance(e)
        ev = d.MeterSample(e.id, self.host.clock(), self._samples(e, TX_UPDATED_MEASURANDS), context)
        await self._adapter.send_meter_sample(ev)
