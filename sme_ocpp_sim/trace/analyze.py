"""Trace analysis (v0.3 plan §3.8): ``ocppsim-analysis/1``.

:func:`analyze` reads a :class:`.format.Trace` (v1 file, v0 ``frames.jsonl`` or the in-memory frame log) and
returns a JSON-ready report; :func:`to_text` renders the summary printed by ``sme-ocpp-sim analyze``. Only the
frames (and v1 events) are looked at - never the simulator state - so a trace of a real charger reads the same.

Report sections (per charger):

* ``actions``: per ``direction`` (``tx`` = CALLs the charger sent, ``rx`` = CALLs the CSMS sent) and action: CALL
  count, CALLRESULT / CALLERROR (by error code), ``unanswered`` (no response in the trace) and the response
  latency p50 / p95 / max in ms (CALL ``ts`` -> response ``ts``, paired by uid in the other direction).
* ``connection``: ``connected`` / ``disconnected`` counts and the connected durations (v1 events only), the
  BootNotification result sequence and the Heartbeat interval distribution (s).
* ``transactions``: start / end, duration, delta meter (Wh), stop reason; 1.6 Start/StopTransaction, 2.0.1 / 2.1
  TransactionEvent with ``seqNo`` gaps and duplicates.
* ``anomalies``: unparseable frames, responses without a CALL (``unpaired``), CALL uids used twice
  (``duplicate_uid``), a CALL sent while an earlier one of the same side was still open (``concurrent_call``).
* ``smart_charging``: timeline of the profile CALLs the CSMS sent (Set / Clear ChargingProfile,
  UpdateDynamicSchedule) with their answer status.

Times are wall seconds (frame ``ts``); latencies are in ms. Durations of transactions use the payload timestamps
(the wire time the CSMS saw) and fall back to the frame ``ts``.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .format import Trace

FORMAT = "ocppsim-analysis/1"
SMART_CHARGING_ACTIONS = frozenset({"SetChargingProfile", "ClearChargingProfile", "UpdateDynamicSchedule"})
_ENERGY = "Energy.Active.Import.Register"
_UNITS = {"Wh": 1.0, "kWh": 1000.0}


def percentile(values: List[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (``q`` in 0..100) of ``values``; ``None`` for no values."""
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(q / 100.0 * len(s)))
    return s[min(k, len(s)) - 1]


def _dist(values: List[float], digits: int = 1) -> Dict[str, Any]:
    r = (lambda v: None if v is None else round(v, digits))
    return {"count": len(values), "min": r(min(values)) if values else None, "p50": r(percentile(values, 50)),
            "p95": r(percentile(values, 95)), "max": r(max(values)) if values else None}


def _parse(raw: Any) -> Optional[list]:
    try:
        f = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return f if isinstance(f, list) and f and f[0] in (2, 3, 4) else None


def _ts(text: Any) -> Optional[float]:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _wh(sv: Dict[str, Any]) -> Optional[float]:
    """Wh of an energy register sampled value (2.0.1: ``unitOfMeasure``; 1.6: ``unit`` / string ``value``)."""
    try:
        v = float(sv.get("value"))
    except (TypeError, ValueError):
        return None
    uom = sv.get("unitOfMeasure") if isinstance(sv.get("unitOfMeasure"), dict) else {}
    unit = uom.get("unit") or sv.get("unit") or "Wh"
    mult = uom.get("multiplier") or 0
    return v * _UNITS.get(unit, 1.0) * (10 ** mult if isinstance(mult, int) else 1)


def _energy(meter_values: Any) -> List[float]:
    out = []
    for mv in meter_values if isinstance(meter_values, list) else ():
        for sv in (mv.get("sampledValue") or []) if isinstance(mv, dict) else ():
            if isinstance(sv, dict) and (sv.get("measurand") or _ENERGY) == _ENERGY and sv.get("phase") is None:
                wh = _wh(sv)
                if wh is not None:
                    out.append(wh)
    return out


class _Charger:
    def __init__(self, cid: str) -> None:
        self.cid = cid
        self.actions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.open: Dict[Tuple[str, str], Dict[str, Any]] = {}   # (call direction, uid) -> call info
        self.seen_uids: Dict[str, set] = {"tx": set(), "rx": set()}
        self.anomalies: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.boots: List[Dict[str, Any]] = []
        self.heartbeats: List[float] = []
        self.tx16: Dict[Any, Dict[str, Any]] = {}
        self.tx16_pending: Dict[str, Dict[str, Any]] = {}       # StartTransaction uid -> transaction
        self.tx201: Dict[str, Dict[str, Any]] = {}
        self.profiles: List[Dict[str, Any]] = []
        self.connects: List[float] = []
        self.disconnects: List[float] = []
        self.up_s: List[float] = []

    def stat(self, direction: str, action: str) -> Dict[str, Any]:
        key = (direction, action)
        if key not in self.actions:
            self.actions[key] = {"calls": 0, "results": 0, "errors": {}, "latency_ms": []}
        return self.actions[key]


def _new_tx(**kw: Any) -> Dict[str, Any]:
    return {"transaction_id": None, "connector": None, "started": None, "ended": None, "duration_s": None,
            "meter_start_wh": None, "meter_stop_wh": None, "delta_wh": None, "stop_reason": None, "id_tag": None,
            **kw}


def _on_call(c: _Charger, f: Dict[str, Any], frame: list) -> None:
    d, uid, action, payload = f["direction"], frame[1], frame[2], frame[3] if len(frame) > 3 else None
    st = c.stat(d, str(action))
    st["calls"] += 1
    if uid in c.seen_uids[d]:
        c.anomalies["duplicate_uid"].append({"seq": f["seq"], "uid": uid, "action": action})
    c.seen_uids[d].add(uid)
    if any(k[0] == d for k in c.open):
        c.anomalies["concurrent_call"].append({"seq": f["seq"], "uid": uid, "action": action,
                                               "open": [v["action"] for k, v in c.open.items() if k[0] == d]})
    p = payload if isinstance(payload, dict) else {}
    info = {"action": action, "ts": f["ts"], "seq": f["seq"], "payload": p}
    c.open[(d, uid)] = info
    if d == "tx" and action == "Heartbeat":
        c.heartbeats.append(f["ts"])
    elif d == "tx" and action == "StartTransaction":
        tx = _new_tx(connector=p.get("connectorId"), started=p.get("timestamp"), meter_start_wh=p.get("meterStart"),
                     id_tag=p.get("idTag"), _t0=_ts(p.get("timestamp")) or f["ts"])
        c.tx16_pending[uid] = tx
    elif d == "tx" and action == "StopTransaction":
        tid = p.get("transactionId")
        tx = c.tx16.get(tid)
        if tx is None:
            tx = c.tx16[tid] = _new_tx(transaction_id=tid)
        tx.update(ended=p.get("timestamp"), meter_stop_wh=p.get("meterStop"), stop_reason=p.get("reason") or "Local",
                  _t1=_ts(p.get("timestamp")) or f["ts"])
    elif d == "tx" and action == "TransactionEvent":
        _on_tx_event(c, f, p)
    elif d == "rx" and action in SMART_CHARGING_ACTIONS:
        prof = p.get("csChargingProfiles") or p.get("chargingProfile") or {}
        crit = p.get("chargingProfileCriteria") or {}
        c.profiles.append({"ts": f["ts"], "seq": f["seq"], "action": action,
                           "connector": p.get("connectorId", p.get("evseId")),
                           "profile_id": prof.get("chargingProfileId", prof.get("id", p.get("id",
                                                                                         p.get("chargingProfileId")))),
                           "purpose": (prof.get("chargingProfilePurpose") or crit.get("chargingProfilePurpose")
                                       or p.get("chargingProfilePurpose")),
                           "stack_level": next((x.get("stackLevel") for x in (prof, crit, p)
                                                if x.get("stackLevel") is not None), None),
                           "status": None, "_uid": uid})


def _on_tx_event(c: _Charger, f: Dict[str, Any], p: Dict[str, Any]) -> None:
    info = p.get("transactionInfo") or {}
    tid = info.get("transactionId")
    if tid is None:
        return
    tx = c.tx201.get(tid)
    if tx is None:
        tx = c.tx201[tid] = _new_tx(transaction_id=tid, _seqnos=[], _energy=[])
    evse = p.get("evse") or {}
    if tx["connector"] is None and evse.get("id") is not None:
        tx["connector"] = evse.get("id")
    if isinstance(p.get("seqNo"), int):
        tx["_seqnos"].append(p["seqNo"])
    tx["_energy"].extend(_energy(p.get("meterValue")))
    t = _ts(p.get("timestamp")) or f["ts"]
    kind = p.get("eventType")
    if kind == "Started" or tx["started"] is None:
        tx["started"], tx["_t0"] = tx["started"] or p.get("timestamp"), tx.get("_t0", t)
        if tx.get("id_tag") is None and isinstance(p.get("idToken"), dict):
            tx["id_tag"] = p["idToken"].get("idToken")
    if kind == "Ended":
        tx["ended"], tx["_t1"] = p.get("timestamp"), t
        tx["stop_reason"] = info.get("stoppedReason") or "Local"


def _on_response(c: _Charger, f: Dict[str, Any], frame: list) -> None:
    call_dir = "rx" if f["direction"] == "tx" else "tx"
    uid = frame[1]
    call = c.open.pop((call_dir, uid), None)
    if call is None:
        c.anomalies["unpaired"].append({"seq": f["seq"], "uid": uid, "message_type": f.get("message_type")})
        return
    action = call["action"]
    st = c.stat(call_dir, str(action))
    st["latency_ms"].append((f["ts"] - call["ts"]) * 1000.0)
    payload = frame[2] if frame[0] == 3 and len(frame) > 2 and isinstance(frame[2], dict) else {}
    if frame[0] == 3:
        st["results"] += 1
    else:
        code = str(frame[2]) if len(frame) > 2 else "?"
        st["errors"][code] = st["errors"].get(code, 0) + 1
    status = payload.get("status") if frame[0] == 3 else f"CALLERROR {frame[2] if len(frame) > 2 else '?'}"
    if call_dir == "tx" and action == "BootNotification":
        c.boots.append({"ts": f["ts"], "seq": f["seq"], "status": status, "interval": payload.get("interval")})
    elif call_dir == "tx" and action == "StartTransaction":
        tx = c.tx16_pending.pop(uid, None)
        if tx is not None and frame[0] == 3:
            tid = payload.get("transactionId")
            tx["transaction_id"] = tid
            tx["id_tag_status"] = (payload.get("idTagInfo") or {}).get("status")
            if tid in c.tx16:  # a Stop seen first (out of order): merge
                tx.update({k: v for k, v in c.tx16[tid].items() if v is not None})
            c.tx16[tid] = tx
    elif call_dir == "rx" and action in SMART_CHARGING_ACTIONS:
        for pr in reversed(c.profiles):
            if pr.get("_uid") == uid:
                pr["status"] = status
                break


def _finish_tx(tx: Dict[str, Any], version: str) -> Dict[str, Any]:
    out = {k: v for k, v in tx.items() if not k.startswith("_")}
    if version == "2.0.1":
        energy = tx.get("_energy") or []
        if energy:
            out["meter_start_wh"], out["meter_stop_wh"] = energy[0], energy[-1]
        seqs = tx.get("_seqnos") or []
        seen, dups = set(), []
        for s in seqs:
            if s in seen:
                dups.append(s)
            seen.add(s)
        top = max(seen) if seen else -1
        out["seq_no"] = {"count": len(seqs), "gaps": [n for n in range(0, top + 1) if n not in seen],
                         "duplicates": sorted(set(dups))}
    if out.get("meter_start_wh") is not None and out.get("meter_stop_wh") is not None:
        out["delta_wh"] = round(float(out["meter_stop_wh"]) - float(out["meter_start_wh"]), 3)
    if tx.get("_t0") is not None and tx.get("_t1") is not None:
        out["duration_s"] = round(tx["_t1"] - tx["_t0"], 3)
    out["complete"] = out["started"] is not None and out["ended"] is not None
    return out


def _events(c: _Charger, events: Iterable[Dict[str, Any]], end_ts: Optional[float]) -> None:
    up_since: Optional[float] = None
    for e in events:
        if e.get("event") == "connected":
            c.connects.append(e["ts"])
            up_since = e["ts"]
        elif e.get("event") == "disconnected":
            c.disconnects.append(e["ts"])
            if up_since is not None:
                c.up_s.append(e["ts"] - up_since)
                up_since = None
    if up_since is not None and end_ts is not None and end_ts >= up_since:
        c.up_s.append(end_ts - up_since)   # still connected at the end of the trace


def analyze(trace: Trace, charger: Optional[str] = None) -> Dict[str, Any]:
    """The ``ocppsim-analysis/1`` report of ``trace`` (only ``charger`` when given)."""
    per: Dict[str, _Charger] = {}
    frames = [f for f in trace.frames if charger is None or f.get("charger") == charger]
    unparseable: Dict[str, List[int]] = defaultdict(list)
    for f in frames:
        cid = str(f.get("charger"))
        c = per.get(cid) or per.setdefault(cid, _Charger(cid))
        frame = _parse(f.get("raw"))
        if frame is None or len(frame) < 2:
            unparseable[cid].append(f.get("seq"))
            continue
        if frame[0] == 2 and len(frame) >= 3:
            _on_call(c, f, frame)
        elif frame[0] in (3, 4):
            _on_response(c, f, frame)
        else:
            unparseable[cid].append(f.get("seq"))
    events = [e for e in trace.events if charger is None or e.get("charger") == charger]
    by_charger: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e.get("charger") is not None:
            by_charger[str(e["charger"])].append(e)
            per.setdefault(str(e["charger"]), _Charger(str(e["charger"])))
    end_ts = max([f["ts"] for f in frames] + [e["ts"] for e in events], default=None)
    start_ts = min([f["ts"] for f in frames] + [e["ts"] for e in events], default=None)

    chargers: Dict[str, Any] = {}
    for cid in sorted(per):
        c = per[cid]
        _events(c, by_charger.get(cid, ()), end_ts)
        has201 = any(k[1] == "TransactionEvent" for k in c.actions)
        version = trace.ocpp_version(cid) or ("2.0.1" if has201 else "1.6" if c.actions else None)
        actions: Dict[str, Dict[str, Any]] = {"tx": {}, "rx": {}}
        for (d, a), st in sorted(c.actions.items()):
            unanswered = sum(1 for k, v in c.open.items() if k[0] == d and v["action"] == a)
            lat = _dist(st["latency_ms"])
            actions[d][a] = {"calls": st["calls"], "results": st["results"], "errors": dict(sorted(st["errors"].items())),
                             "unanswered": unanswered, "latency_ms": {k: lat[k] for k in ("p50", "p95", "max")}}
        hb = [round(b - a, 3) for a, b in zip(c.heartbeats, c.heartbeats[1:])]
        txs = [_finish_tx(t, "1.6") for t in c.tx16.values()] + [_finish_tx(t, "1.6") for t in c.tx16_pending.values()]
        txs += [_finish_tx(t, "2.0.1") for t in c.tx201.values()]
        txs.sort(key=lambda t: (t.get("started") or "", str(t.get("transaction_id"))))
        anomalies = {"unparseable": unparseable.get(cid, []), "unpaired": c.anomalies["unpaired"],
                     "duplicate_uid": c.anomalies["duplicate_uid"], "concurrent_call": c.anomalies["concurrent_call"],
                     "unanswered": [{"seq": v["seq"], "uid": k[1], "action": v["action"], "direction": k[0]}
                                    for k, v in sorted(c.open.items(), key=lambda kv: kv[1]["seq"])]}
        chargers[cid] = {
            "ocpp_version": version,
            "frames": sum(1 for f in frames if str(f.get("charger")) == cid),
            "actions": actions,
            "connection": {"connected": len(c.connects), "disconnected": len(c.disconnects),
                           "connected_s": _dist(c.up_s), "boots": c.boots,
                           "heartbeat_interval_s": _dist(hb)},
            "transactions": txs,
            "anomalies": anomalies,
            "smart_charging": [{k: v for k, v in p.items() if not k.startswith("_")} for p in c.profiles],
        }
    hdr = trace.header or {}
    return {"format": FORMAT, "trace": {"version": trace.version, "tool_version": hdr.get("tool_version"),
                                        "created": hdr.get("created"), "parts": trace.parts,
                                        "frames": len(frames), "events": len(events), "dropped": trace.dropped,
                                        "complete": trace.version == 0 or trace.footer is not None,
                                        "bad_lines": trace.bad_lines,
                                        "start_ts": start_ts, "end_ts": end_ts,
                                        "span_s": round(end_ts - start_ts, 3) if start_ts is not None else None},
            "chargers": chargers}


# ------------------------------------------------------------------ text summary

def _fmt(v: Any) -> str:
    return "-" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


def to_text(report: Dict[str, Any]) -> str:
    t = report["trace"]
    lines = [f"trace v{t['version']}: {t['frames']} frame(s), {t['events']} event(s), span {_fmt(t['span_s'])} s"
             + (f", {t['dropped']} dropped" if t["dropped"] else "")
             + ("" if t["complete"] else ", no footer (recording not closed)")
             + (f", {len(t['bad_lines'])} bad line(s)" if t["bad_lines"] else "")]
    for cid, c in report["chargers"].items():
        lines.append("")
        lines.append(f"== {cid} (OCPP {c['ocpp_version'] or '?'}, {c['frames']} frame(s))")
        for d, title in (("tx", "charger -> CSMS"), ("rx", "CSMS -> charger")):
            if not c["actions"][d]:
                continue
            lines.append(f"  {title}:")
            lines.append(f"    {'action':<34}{'calls':>6}{'ok':>6}{'err':>6}{'open':>6}{'p50 ms':>9}{'p95 ms':>9}"
                         f"{'max ms':>9}")
            for a, s in c["actions"][d].items():
                err = sum(s["errors"].values())
                lat = s["latency_ms"]
                lines.append(f"    {a:<34}{s['calls']:>6}{s['results']:>6}{err:>6}{s['unanswered']:>6}"
                             f"{_fmt(lat['p50']):>9}{_fmt(lat['p95']):>9}{_fmt(lat['max']):>9}")
                if s["errors"]:
                    lines.append("      errors: " + ", ".join(f"{k} x{v}" for k, v in s["errors"].items()))
        cn = c["connection"]
        hb = cn["heartbeat_interval_s"]
        lines.append(f"  connection: {cn['connected']} connect(s), {cn['disconnected']} disconnect(s)"
                     + (f", connected p50 {_fmt(cn['connected_s']['p50'])} s" if cn["connected_s"]["count"] else ""))
        if cn["boots"]:
            lines.append("  boot: " + " -> ".join(_fmt(b["status"]) for b in cn["boots"]))
        if hb["count"]:
            lines.append(f"  heartbeat interval: p50 {_fmt(hb['p50'])} s, min {_fmt(hb['min'])} s, "
                         f"max {_fmt(hb['max'])} s ({hb['count']})")
        for tx in c["transactions"]:
            extra = ""
            if "seq_no" in tx and (tx["seq_no"]["gaps"] or tx["seq_no"]["duplicates"]):
                extra = f", seqNo gaps {tx['seq_no']['gaps']} duplicates {tx['seq_no']['duplicates']}"
            lines.append(f"  transaction {_fmt(tx['transaction_id'])} (connector {_fmt(tx['connector'])}): "
                         f"{_fmt(tx['duration_s'])} s, {_fmt(tx['delta_wh'])} Wh, "
                         f"reason {_fmt(tx['stop_reason']) if tx['complete'] else 'open'}{extra}")
        an = c["anomalies"]
        found = {k: len(v) for k, v in an.items() if v}
        if found:
            lines.append("  anomalies: " + ", ".join(f"{k} {v}" for k, v in found.items()))
        for p in c["smart_charging"]:
            lines.append(f"  profile {p['action']} id {_fmt(p['profile_id'])} purpose {_fmt(p['purpose'])} "
                         f"stack {_fmt(p['stack_level'])} connector {_fmt(p['connector'])}: {_fmt(p['status'])}")
    return "\n".join(lines) + "\n"
