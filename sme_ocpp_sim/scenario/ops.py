"""Action layer (v0.3 plan §3.2): the operator actions on a charger, one function per action.

The web routes (and through them the MCP tools), and later the scenario runner and the traffic generator, all go
through these functions, so UI / API / MCP / scenarios behave the same. Each action returns the JSON body the
matching ``POST /api/chargers/{id}/<action>`` route answers (``{"ok": ..., ..., "last_seq": n}``; ``last_seq``
is the frame-log seq before the action) and reports a rejected action
as :class:`OpError`, whose ``kind`` the web layer maps to an HTTP status (:data:`HTTP_STATUS`).

==============  =======================================  ==========================================
action          function                                 route
==============  =======================================  ==========================================
``plug``        :func:`plug`                             ``/chargers/{id}/plug``
``unplug``      :func:`unplug`                           ``/chargers/{id}/unplug``
``swipe``       :func:`swipe`                            ``/chargers/{id}/swipe``
``start``       :func:`start` (plug + swipe)             ``/chargers/{id}/start``
``stop``        :func:`stop`                             ``/chargers/{id}/stop``
``fault``       :func:`fault`                            ``/chargers/{id}/fault``
``clear_fault`` :func:`clear_fault`                      ``/chargers/{id}/clear-fault``
``connect``     :func:`connect`                          ``/chargers/{id}/connect`` (and bulk)
``disconnect``  :func:`disconnect`                       ``/chargers/{id}/disconnect`` (and bulk)
``drop``        :func:`drop`                             ``/chargers/{id}/drop``
``impair``      :func:`impair`                           ``PUT /chargers/{id}/impairments``
``send``        :func:`send`                             ``/chargers/{id}/send``
``set_ev``      :func:`set_ev`                           (scenario only)
==============  =======================================  ==========================================

``wait`` and ``log`` belong to the scenario runner (:mod:`.runner`), they do not touch a charger. Request validation (field types, ranges) stays with
the callers' models (the web request bodies); the actions check what depends on the charger (connector range,
fault codes of its version, transaction state).
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import random
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from .. import framelog
from ..charging.ev import PRESETS, Vehicle
from ..config import ChargerConfig, ImpairmentConfig, SimConfig
from ..csms.handlers import SPECS
from ..engine import OcppEngine, OperationNotPossible
from ..rpc.errors import CallError

#: Largest serialized ``send`` payload (characters).
MAX_INJECT_BYTES = 1_000_000

ErrorKind = Literal["invalid", "not_found", "conflict", "too_large", "timeout"]
#: ``OpError.kind`` -> HTTP status of the web routes.
HTTP_STATUS: Dict[str, int] = {"invalid": 400, "not_found": 404, "conflict": 409, "too_large": 413, "timeout": 504}
#: ``send(payload=...)`` default: no payload given (an explicit ``None`` is a given payload).
UNSET: Any = object()


class OpError(Exception):
    """A rejected action. ``detail`` is the message or the original exception (a pydantic ``ValidationError``
    keeps its structure for the web layer's compact rendering)."""

    def __init__(self, kind: ErrorKind, detail: Union[str, BaseException]) -> None:
        super().__init__(str(detail))
        self.kind = kind
        self.detail = detail


# ------------------------------------------------------------------ lookups

def runtime(engine: OcppEngine, cid: str):
    """The runtime of ``cid``; ``not_found`` for an unknown charger."""
    try:
        return engine.charger(cid)
    except KeyError:
        raise OpError("not_found", f"unknown charger {cid!r}") from None


def connector_ok(rt, connector: int) -> None:
    if not 1 <= connector <= rt.connector_count:
        raise OpError("invalid", f"no connector {connector} (1..{rt.connector_count})")


def charger_cfg(engine: OcppEngine, cid: str) -> Tuple[int, ChargerConfig]:
    """Index and config of charger ``cid`` in the running config; ``not_found`` when there is none."""
    for i, c in enumerate(engine.config.chargers):
        if c.id == cid:
            return i, c
    raise OpError("not_found", f"unknown charger {cid!r}")


async def apply_config(engine: OcppEngine, data: dict) -> None:
    """Validate ``data`` (a whole config dump) and reload the engine with it; any rejection is ``invalid``."""
    try:
        cfg = SimConfig.model_validate(data)
    except ValueError as e:  # pydantic ValidationError
        raise OpError("invalid", e) from None
    try:
        await engine.reload(cfg)
    except Exception as e:  # noqa: BLE001 - build / apply errors: the old config keeps running
        raise OpError("invalid", e) from None


def _ready(engine: OcppEngine, cid: str, connector: int):
    rt = runtime(engine, cid)
    connector_ok(rt, connector)
    return rt


def _seq(fn):
    """Add ``last_seq`` to the action's answer: the frame-log seq captured *before* the action, so a client can
    pass it as ``since`` to ``POST /api/frames/expect`` (or ``GET /api/frames``) and see every frame the action
    caused."""
    sig = inspect.signature(fn)
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(engine: OcppEngine, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            seq = engine.frame_log.last_seq
            out = {**await fn(engine, *args, **kwargs), "last_seq": seq}
            _trace(engine, fn.__name__, sig, args, kwargs, seq)
            return out
    else:
        @functools.wraps(fn)
        def wrapper(engine: OcppEngine, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[misc]
            seq = engine.frame_log.last_seq
            out = {**fn(engine, *args, **kwargs), "last_seq": seq}
            _trace(engine, fn.__name__, sig, args, kwargs, seq)
            return out
    return wrapper


def _trace(engine: OcppEngine, name: str, sig: inspect.Signature, args: tuple, kwargs: dict, seq: int) -> None:
    """Record a successful action as a trace event (v0.3 M5): ``event`` = the function name, ``detail`` = its
    JSON-able arguments plus ``since_seq`` (the frame seq before the action, so a reader can order it against the
    frames it caused). Recorded after the action (a failed one is not an event); never raises."""
    recorders = getattr(engine, "recorders", None)
    if not recorders:
        return
    try:
        bound = sig.bind(engine, *args, **kwargs)
        bound.apply_defaults()
        detail: Dict[str, Any] = {"since_seq": seq}
        cid = None
        for k, v in list(bound.arguments.items())[1:]:
            if k == "cid":
                cid = v
            elif isinstance(v, (str, int, float, bool, list, dict)) or v is None:
                detail[k] = v
            elif hasattr(v, "model_dump"):
                detail[k] = v.model_dump(mode="json")
        if name == "send":   # a payload may carry a secret: mask it exactly as the frame log does
            if "payload" in detail:
                detail["payload"] = framelog.mask_payload(detail.get("action"), detail["payload"])
            if isinstance(detail.get("raw"), str):
                detail["raw"] = framelog.mask_frame(detail["raw"])
        engine.trace_event(cid, name, detail)
    except Exception:  # noqa: BLE001 - recording must never fail an action
        logging.getLogger(__name__).exception("trace event %s failed", name)


# ------------------------------------------------------------------ connection

async def _engine_op(fn, cid: str) -> bool:
    try:
        return await fn(cid)
    except KeyError:
        raise OpError("not_found", f"unknown charger {cid!r}") from None
    except OperationNotPossible as e:
        raise OpError("invalid", e) from None


@_seq
async def connect(engine: OcppEngine, cid: str) -> Dict[str, Any]:
    """Start the connection loop again; ``changed`` = it was not running."""
    return {"ok": True, "changed": await _engine_op(engine.connect, cid)}


@_seq
async def disconnect(engine: OcppEngine, cid: str) -> Dict[str, Any]:
    """Close the connection and keep it closed; ``changed`` = it was running."""
    return {"ok": True, "changed": await _engine_op(engine.disconnect, cid)}


@_seq
async def connect_many(engine: OcppEngine, ids: List[str], *, up: bool) -> Dict[str, Any]:
    """:func:`connect` (``up``) or :func:`disconnect` for each of ``ids``, in order."""
    fn = engine.connect if up else engine.disconnect
    return {"ok": True, "changed": {cid: await _engine_op(fn, cid) for cid in ids}}


@_seq
def drop(engine: OcppEngine, cid: str, mode: Optional[str] = None,
         reconnect_after_s: Optional[float] = None) -> Dict[str, Any]:
    """Drop the connection now; both default to the charger's ``impairments`` (``drop_mode`` / ``down_s``)."""
    rt = runtime(engine, cid)
    try:
        rt.drop(mode, reconnect_after_s)
    except ConnectionError as e:
        raise OpError("conflict", e) from None
    imp = rt.impairments
    return {"ok": True, "mode": mode or imp.drop_mode,
            "reconnect_after_s": imp.down_s if reconnect_after_s is None else reconnect_after_s}


@_seq
async def impair(engine: OcppEngine, cid: str, impairments: Union[ImpairmentConfig, dict]) -> Dict[str, Any]:
    """Replace the charger's ``impairments`` block (a reload; applied live, the charger keeps its connection)."""
    if not isinstance(impairments, ImpairmentConfig):
        try:
            impairments = ImpairmentConfig.model_validate(impairments)
        except ValueError as e:
            raise OpError("invalid", e) from None
    i, c = charger_cfg(engine, cid)
    data = engine.config.model_dump()
    data["chargers"][i] = {**c.model_dump(), "impairments": impairments.model_dump()}
    await apply_config(engine, data)  # impairments is not part of the restart key: no reconnect
    return {"ok": True, **charger_cfg(engine, cid)[1].impairments.model_dump(mode="json")}


# ------------------------------------------------------------------ connector

@_seq
async def plug(engine: OcppEngine, cid: str, connector: int = 1) -> Dict[str, Any]:
    """Plug the EV cable in."""
    await _ready(engine, cid, connector).plug(connector)
    return {"ok": True}


@_seq
async def unplug(engine: OcppEngine, cid: str, connector: int = 1) -> Dict[str, Any]:
    """Unplug the EV cable (ends a transaction)."""
    await _ready(engine, cid, connector).unplug(connector)
    return {"ok": True}


@_seq
async def swipe(engine: OcppEngine, cid: str, connector: int = 1, id_tag: Optional[str] = None) -> Dict[str, Any]:
    """Present an id tag (start / stop like an RFID reader)."""
    rt = _ready(engine, cid, connector)
    if id_tag is None:
        raise OpError("invalid", "id_tag: required")
    status = await rt.swipe(connector, id_tag)
    return {"ok": True, "status": status.value}


@_seq
async def start(engine: OcppEngine, cid: str, connector: int = 1, id_tag: Optional[str] = None) -> Dict[str, Any]:
    """Plug in and present an id tag (default: the charger's first configured one).

    Check-then-act: the transaction / fault checks and the plug + swipe that follow are not atomic (a CSMS
    RemoteStart or another request may run in between); the runtime then answers by its own rules, the checks
    only give the common mistakes a clear error."""
    rt = _ready(engine, cid, connector)
    tag = id_tag or (rt.cfg.id_tags[0] if rt.cfg.id_tags else None)
    if tag is None:
        raise OpError("invalid", "id_tag: required (the charger has no id_tags configured)")
    if rt.has_transaction(connector):
        raise OpError("invalid", f"connector {connector} already has a transaction")
    if rt.faulted(connector):
        raise OpError("invalid", f"connector {connector} is faulted")
    await rt.plug(connector)
    status = await rt.swipe(connector, tag)
    return {"ok": True, "status": status.value, "id_tag": tag}


@_seq
async def stop(engine: OcppEngine, cid: str, connector: int = 1) -> Dict[str, Any]:
    """Stop the transaction locally (reason Local)."""
    if not await _ready(engine, cid, connector).stop_tx(connector):
        raise OpError("invalid", f"connector {connector} has no transaction")
    return {"ok": True}


@_seq
async def fault(engine: OcppEngine, cid: str, connector: int = 1, error_code: str = "OtherError") -> Dict[str, Any]:
    """Fault a connector (StatusNotification Faulted)."""
    rt = _ready(engine, cid, connector)
    if error_code not in rt.fault_codes:
        raise OpError("invalid", f"error_code {error_code!r} is not a fault code of OCPP {rt.adapter.version}")
    await rt.fault(connector, error_code)
    return {"ok": True}


@_seq
async def clear_fault(engine: OcppEngine, cid: str, connector: int = 1) -> Dict[str, Any]:
    """Clear a connector fault."""
    if not await _ready(engine, cid, connector).clear_fault(connector):
        raise OpError("invalid", f"connector {connector} is not faulted")
    return {"ok": True}


# ------------------------------------------------------------------ frame injection

@_seq
async def send(engine: OcppEngine, cid: str, *, action: Optional[str] = None, payload: Any = UNSET,
               raw: Optional[str] = None, validate: bool = True, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Inject a CALL (``action`` + ``payload``) or a raw frame (``raw``); no station state change.

    A CALLERROR answer is a result (``ok: false`` with ``error``), not an :class:`OpError`. Giving both ``raw``
    and ``action`` / ``payload`` is ``invalid``; a CALL without ``payload`` sends ``{}``."""
    rt = runtime(engine, cid)
    if raw is not None:
        if action is not None or payload is not UNSET:
            raise OpError("invalid", "give either raw or action / payload, not both")
        try:
            await rt.inject_raw(raw)
        except (ConnectionError, ValueError) as e:  # not connected / uniqueId of a pending station CALL
            raise OpError("conflict", e) from None
        return {"ok": True, "raw": True}
    if action is None:
        raise OpError("invalid", "action (or raw): required")
    payload = {} if payload is UNSET else payload
    if len(json.dumps(payload, separators=(",", ":"))) > MAX_INJECT_BYTES:
        raise OpError("too_large", f"payload: larger than {MAX_INJECT_BYTES} characters serialized")
    spec = SPECS.get(rt.adapter.version)
    if validate:
        if spec is None:
            raise OpError("invalid", f"no schema check for OCPP {rt.adapter.version}; send with validate: false")
        if action not in spec.cp_actions:
            raise OpError("invalid", f"{action!r} is not a CP -> CSMS action of OCPP {spec.version}")
        try:
            spec.codec.parse(action, payload, "request", strict=True)
        except CallError as e:
            raise OpError("invalid", f"{e.code}: {e.description}") from None
    try:
        result = await rt.inject_call(action, payload, timeout=timeout)
    except ConnectionError as e:
        raise OpError("conflict", e) from None
    except TimeoutError:
        raise OpError("timeout", f"{action}: the CSMS did not answer in time") from None
    except CallError as e:
        return {"ok": False, "error": {"code": e.code, "description": e.description, "details": e.details}}
    out: Dict[str, Any] = {"ok": True, "result": result}
    if validate and spec is not None:
        try:
            spec.codec.parse(action, result, "response", strict=False)
        except CallError as e:
            out["warning"] = f"the answer does not match the response schema: {e.code}: {e.description}"
    return out


# ------------------------------------------------------------------ vehicle

@_seq
def set_ev(engine: OcppEngine, cid: str, connector: int = 1, ev_model: Optional[str] = None,
           initial_soc: Optional[float] = None, target_soc: Optional[float] = None) -> Dict[str, Any]:
    """Set the EV of the connector's next transaction (used once). ``ev_model``: an id of the charger's vehicle
    pool, of ``ev_models`` or a preset (default: the charger's current model); the SoC values default to the pool
    entry's distribution (drawn now) or the charger's ``session`` values."""
    rt = _ready(engine, cid, connector)
    for name, v in (("initial_soc", initial_soc), ("target_soc", target_soc)):
        if v is not None and not 0 <= v <= 100:
            raise OpError("invalid", f"{name}: must be 0..100")
    entry = None
    if rt.pool is not None:
        want = ev_model or rt.ev.id
        entry = next((e for e in rt.pool.entries if e.ev.id == want), None)
        model = entry.ev if entry is not None else None
    else:
        model = rt.ev if ev_model in (None, rt.ev.id) else None
    if model is None:
        model = next((e.to_model() for e in engine.config.ev_models if e.id == ev_model), None) or PRESETS.get(ev_model)
    if model is None:
        raise OpError("invalid", f"ev_model {ev_model!r}: not a preset, ev_models entry or pool vehicle")
    rng = random.Random(f"set_ev:{cid}:{connector}")  # deterministic when only the model is set
    ini = initial_soc if initial_soc is not None else (
        entry.initial_soc.sample(rng) if entry is not None else rt.cfg.session.initial_soc)
    tgt = target_soc if target_soc is not None else (
        entry.target_soc.sample(rng) if entry is not None else rt.cfg.session.target_soc)
    rt.next_vehicle[connector] = Vehicle(model, float(ini), float(tgt))
    return {"ok": True, "ev_model": model.id, "initial_soc": float(ini), "target_soc": float(tgt)}
