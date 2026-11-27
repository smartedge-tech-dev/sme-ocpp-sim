"""Mini CSMS: what is recorded per connected charging station (one :class:`CpSession` per WebSocket connection).

Recorded: the handshake (version, remote address, whether Basic Auth was sent), the last BootNotification and its
answer, heartbeats, the last status per connector (1.6 key ``"<connectorId>"``, 2.0.1 ``"<evseId>/<connectorId>"``),
the transactions (bounded), the newest meter values (bounded), a count and the last payload per inbound action,
payload warnings (tolerant mode) and the CALLs the CSMS sent (manual and plan). All payloads are kept in wire form
(camelCase JSON). Times are wall-clock seconds since the epoch.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Any, Callable, Dict, List, Optional

MAX_TRANSACTIONS = 100
MAX_METER_VALUES = 50
MAX_CALLS = 50
MAX_WARNINGS = 50


def _sampled(mv: Any) -> Dict[str, Any]:
    """``{"<measurand>[.<phase>]": "<value> <unit>"}`` of one MeterValue (both versions)."""
    out: Dict[str, Any] = {}
    for sv in (mv.get("sampledValue") or []) if isinstance(mv, dict) else []:
        if not isinstance(sv, dict):
            continue
        name = sv.get("measurand") or "Energy.Active.Import.Register"
        if sv.get("phase"):
            name += f".{sv['phase']}"
        unit = sv.get("unit")
        if unit is None and isinstance(sv.get("unitOfMeasure"), dict):
            unit = sv["unitOfMeasure"].get("unit")
        out[name] = f"{sv.get('value')} {unit}" if unit else sv.get("value")
    return out


class CpSession:
    def __init__(self, cp_id: str, version: str, subprotocol: str, *, remote: Optional[str] = None,
                 basic_auth: bool = False, clock: Callable[[], float] = time.time) -> None:
        self.cp_id = cp_id
        self.version = version
        self.subprotocol = subprotocol
        self.remote = remote
        self.basic_auth = basic_auth
        self._clock = clock
        self.connected = True
        self.connected_at = clock()
        self.disconnected_at: Optional[float] = None
        self.boot: Optional[dict] = None
        self.boot_status: Optional[str] = None
        self.boot_at: Optional[float] = None
        self.boots = 0
        self.heartbeats = 0
        self.heartbeat_at: Optional[float] = None
        self.statuses: Dict[str, dict] = {}
        self.transactions: "OrderedDict[str, dict]" = OrderedDict()
        self.meter: deque = deque(maxlen=MAX_METER_VALUES)
        self.counts: Dict[str, int] = {}
        self.last: Dict[str, dict] = {}
        self.warnings: deque = deque(maxlen=MAX_WARNINGS)
        self.calls: deque = deque(maxlen=MAX_CALLS)
        self.plan: List[dict] = []   # filled by the plan runner (its live item states)

    # ------------------------------------------------------------ recording (handlers)

    def count(self, action: str, payload: dict) -> None:
        self.counts[action] = self.counts.get(action, 0) + 1
        self.last[action] = payload

    def warn(self, text: str) -> None:
        self.warnings.append({"ts": self._clock(), "text": text})

    def on_boot(self, payload: dict, status: str) -> None:
        self.boot, self.boot_status, self.boot_at = payload, status, self._clock()
        self.boots += 1

    def on_heartbeat(self) -> None:
        self.heartbeats += 1
        self.heartbeat_at = self._clock()

    def status(self, key: str, status: Any, error_code: Any, timestamp: Any) -> None:
        self.statuses[key] = {"status": status, "error_code": error_code, "timestamp": timestamp,
                              "received_at": self._clock()}

    def has_tx(self, tx_id: str) -> bool:
        return tx_id in self.transactions

    def tx_start(self, tx_id: str, connector: Any, id_tag: Any, meter_start: Any, timestamp: Any) -> None:
        self.transactions[tx_id] = {"id": tx_id, "connector": connector, "id_tag": id_tag, "active": True,
                                    "started": timestamp, "meter_start_wh": meter_start, "stopped": None,
                                    "meter_stop_wh": None, "stop_reason": None, "charging_state": None,
                                    "last_trigger": None, "energy": None}
        while len(self.transactions) > MAX_TRANSACTIONS:
            self.transactions.popitem(last=False)

    def tx_update(self, tx_id: str, *, tag: Any = None, state: Any = None, trigger: Any = None) -> None:
        tx = self.transactions.get(tx_id)
        if tx is None:
            return
        if tag is not None:
            tx["id_tag"] = tag
        if state is not None:
            tx["charging_state"] = state
        if trigger is not None:
            tx["last_trigger"] = trigger

    def tx_stop(self, tx_id: str, meter_stop: Any, timestamp: Any, reason: Any) -> None:
        tx = self.transactions.get(tx_id)
        if tx is None:  # a stop for a transaction started before this connection
            self.tx_start(tx_id, None, None, None, None)
            tx = self.transactions[tx_id]
        tx.update(active=False, stopped=timestamp, meter_stop_wh=meter_stop, stop_reason=reason or "Local")

    def meter_values(self, connector: Any, tx_id: Any, values: List[Any]) -> None:
        for mv in values:
            sampled = _sampled(mv)
            if not sampled:
                continue
            self.meter.append({"received_at": self._clock(), "timestamp": mv.get("timestamp"),
                               "connector": connector, "transaction": None if tx_id is None else str(tx_id),
                               "values": sampled})
            tx = self.transactions.get(str(tx_id)) if tx_id is not None else None
            if tx is not None and "Energy.Active.Import.Register" in sampled:
                tx["energy"] = sampled["Energy.Active.Import.Register"]

    def record_call(self, entry: dict) -> None:
        self.calls.append(entry)

    def closed(self) -> None:
        self.connected = False
        self.disconnected_at = self._clock()

    # ------------------------------------------------------------ views

    def summary(self) -> Dict[str, Any]:
        return {"id": self.cp_id, "ocpp_version": self.version, "connected": self.connected,
                "connected_at": self.connected_at, "disconnected_at": self.disconnected_at,
                "remote": self.remote, "basic_auth": self.basic_auth, "boot_status": self.boot_status,
                "vendor": (self.boot or {}).get("chargePointVendor") or
                ((self.boot or {}).get("chargingStation") or {}).get("vendorName"),
                "model": (self.boot or {}).get("chargePointModel") or
                ((self.boot or {}).get("chargingStation") or {}).get("model"),
                "heartbeat_at": self.heartbeat_at,
                "active_transactions": sum(1 for t in self.transactions.values() if t["active"])}

    def detail(self) -> Dict[str, Any]:
        return {**self.summary(), "subprotocol": self.subprotocol, "boot": self.boot, "boot_at": self.boot_at,
                "boots": self.boots, "heartbeats": self.heartbeats, "statuses": dict(self.statuses),
                "transactions": list(self.transactions.values()), "meter_values": list(self.meter),
                "counts": dict(self.counts), "last": dict(self.last), "warnings": list(self.warnings),
                "calls": list(self.calls), "plan": [dict(p) for p in self.plan]}
