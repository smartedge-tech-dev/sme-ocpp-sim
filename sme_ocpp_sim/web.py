"""FastAPI app (plan §5 M6).

The generic routes (``/``, ``/api/version``, ``/api/state``, ``/api/config*``, ``/api/devices*``,
``/api/simulation``, ``/ws``, the ``/sme-sim-common`` and ``/static`` mounts) come from
``sme_sim_common.web.make_base_app`` (devices = ``chargers``, key = ``id``). This module adds the OCPP routes:

* ``POST /api/chargers/{id}/{connect,disconnect}`` - start / stop the connection loop (engine level; a disconnect
  is sticky until ``connect``, transactions survive it like a network loss);
* ``POST /api/chargers/{id}/{plug,unplug,swipe,start,stop,fault,clear-fault}`` - body ``{"connector": 1, ...}``
  (``id_tag`` for swipe / start, ``error_code`` for fault: a 1.6 ``ChargePointErrorCode``, default
  ``OtherError``; a fault ends a running transaction with reason ``Other`` and reports ``Faulted``, clear-fault
  returns the connector to Available / Preparing);
* ``POST /api/chargers/{id}/datatransfer`` - body ``{"vendor_id", "message_id", "data"}``, CP -> CSMS;
* ``GET /api/chargers/{id}/profiles?connector=`` - installed charging profiles;
* ``GET /api/chargers/{id}/composite?connector=&duration=&unit=`` - the GetCompositeSchedule answer computed
  locally. In OCPP 1.6 the plan's ``evse`` is a connectorId, so the parameter is ``connector`` (``evse`` is
  accepted as an alias; 0 = the whole charge point);
* ``GET /api/frames?since=&charger=&action=&dir=&limit=`` / ``GET /api/frames.jsonl`` - the frame log;
* ``POST /api/chargers/bulk/{connect,disconnect}`` - body ``{"ids": [...]}`` / ``["A", "B"]`` / ``{"all": true}``
  (a charger called ``bulk`` cannot be connected / disconnected through the single route).

Errors: an unknown charger is 404, invalid input or an operation the current state does not allow is 400 (both
``sme_sim_common.web.bad``; request validation errors are 400 too instead of FastAPI's 422).

Secrets: ``/api/state`` and the frame log never contain passwords (frame log: redacted Authorization). The base
config / device routes are wrapped so that every ``password`` they return is :data:`PASSWORD_MASK`; a config sent
back with the mask keeps the stored password (``OcppEngine.reload``). ``POST /api/config/save`` writes through
``config.save_file`` (comments kept) in a worker thread under the engine lock (``OcppEngine.save_config``).

``/ws`` client contract: the first message is ``{"type": "snapshot", "data": <GET /api/state>}``. Then the
engine's pushes follow (``charger_state`` / ``values`` / ``frame``, see ``engine.py``), each with ``push_seq``
counting 1, 2, 3 ... per connection, ``{"type": "log", "event": {...}}`` engine events (numbered by their own
``event.seq``, no ``push_seq``) and ``{"type": "snapshot"}`` again after a config reload. A gap in
``push_seq`` means messages were dropped for a slow client: the next state push is then a full one
(``full: true``) so state heals by itself, or the client may refetch ``/api/state``; lost frames are fetched with
``GET /api/frames?since=<last frame seq seen>``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Literal, Optional, Union

import yaml
from fastapi import APIRouter, Body, FastAPI, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sme_sim_common.web import bad, error_response, make_base_app
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import __version__
from .charging.profiles import ChargingRateUnit
from .config import PASSWORD_MASK, SimConfig
from .engine import OcppEngine
from .protocol.base import DataTransferStatus
from .protocol.v16.enums import ChargePointErrorCode

STATIC = Path(__file__).resolve().parent / "static"
#: Fault codes an operator may set (1.6 ``ChargePointErrorCode`` without ``NoError``).
FAULT_CODES = tuple(e.value for e in ChargePointErrorCode if e is not ChargePointErrorCode.no_error)
MAX_FRAMES = 5000
NDJSON = "application/x-ndjson"


# ------------------------------------------------------------------ request bodies

class ConnectorBody(BaseModel):
    connector: int = Field(1, ge=1)


class TagBody(ConnectorBody):
    id_tag: Optional[str] = Field(None, min_length=1, max_length=20)


class FaultBody(ConnectorBody):
    error_code: str = "OtherError"


class DataTransferBody(BaseModel):
    vendor_id: str = Field(min_length=1, max_length=255)
    message_id: Optional[str] = Field(None, max_length=50)
    data: Optional[str] = None


class BulkBody(BaseModel):
    ids: List[str] = Field(default_factory=list)
    all: bool = False


# ------------------------------------------------------------------ password masking

_PASSWORD_LINE = re.compile(r"(?m)^([ \t]*(?:-[ \t]+)?password[ \t]*:)[ \t]*(?!(?:null|~)?[ \t]*(?:#.*)?$)\S.*$")


def mask_obj(obj: Any) -> Any:
    """``obj`` with every non-null ``password`` value replaced by :data:`PASSWORD_MASK` (dicts / lists)."""
    if isinstance(obj, dict):
        return {k: (PASSWORD_MASK if k == "password" and v is not None else mask_obj(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_obj(v) for v in obj]
    return obj


def _unmasked(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any((k == "password" and v not in (None, PASSWORD_MASK)) or _unmasked(v) for k, v in obj.items())
    return isinstance(obj, list) and any(_unmasked(v) for v in obj)


def mask_text(text: str) -> str:
    """YAML ``text`` with every password masked. Block-style ``password:`` lines are rewritten in place (the rest of
    the line, including a trailing comment, is dropped); if the result still parses to a clear password (flow
    style, multi-line scalars ...) the document is re-dumped from the masked data (comments lost)."""
    out = _PASSWORD_LINE.sub(lambda m: f'{m.group(1)} "{PASSWORD_MASK}"', text)
    try:
        if not _unmasked(yaml.safe_load(out)):
            return out
        return yaml.safe_dump(mask_obj(yaml.safe_load(text)), allow_unicode=True, sort_keys=False)
    except yaml.YAMLError:
        return out


def _masked(endpoint):
    """Wrap a base route so its YAML / JSON body has the passwords masked."""
    async def wrapper(request: Request):
        resp = await endpoint(request)
        media = resp.media_type or ""
        if "yaml" in media:
            return PlainTextResponse(mask_text(resp.body.decode("utf-8")), status_code=resp.status_code,
                                     media_type=media)
        if "json" in media:
            return JSONResponse(mask_obj(json.loads(resp.body)), status_code=resp.status_code)
        return resp
    return wrapper


# ------------------------------------------------------------------ hooks for make_base_app

def duplicate_charger(engine: OcppEngine, dev: dict, new_key: Any, body: dict, *, src_key: str) -> dict:
    """``CP001`` -> the next free ``CP002`` (same digit width); without trailing digits ``X`` -> ``X-2``. An id given
    in the request body is kept."""
    if body.get("id") or body.get("name"):
        return dev
    ids = {c.id for c in engine.config.chargers}
    m = re.match(r"^(.*?)(\d+)$", src_key)
    prefix, n, width = (m.group(1), int(m.group(2)) + 1, len(m.group(2))) if m else (f"{src_key}-", 2, 1)
    while f"{prefix}{n:0{width}d}" in ids:
        n += 1
    return {**dev, "id": f"{prefix}{n:0{width}d}"}


def simulation_hook(engine: OcppEngine, rest: dict) -> None:
    """``POST /api/simulation`` takes only ``running`` / ``time_scale``."""
    if rest:
        raise ValueError(f"unknown field(s) {sorted(rest)}; supported: running, time_scale")
    return None


# ------------------------------------------------------------------ product router

def _router(engine: OcppEngine) -> APIRouter:
    r = APIRouter(prefix="/api", tags=["ocpp"])

    def runtime(cid: str):
        try:
            return engine.charger(cid)
        except KeyError:
            raise bad(404, f"unknown charger {cid!r}") from None

    def conn_ok(rt, connector: int) -> None:
        if not 1 <= connector <= rt.connector_count:
            raise bad(400, f"no connector {connector} (1..{rt.connector_count})")

    async def engine_op(fn, cid: str) -> bool:
        try:
            return await fn(cid)
        except RuntimeError as e:
            raise bad(400, e) from None

    def bulk_ids(body: Union[List[str], BulkBody]) -> List[str]:
        if isinstance(body, BulkBody):
            ids = list(engine.chargers) if body.all else body.ids
        else:
            ids = body
        if not ids:
            raise bad(400, "no chargers given (ids or all: true)")
        unknown = [i for i in ids if i not in engine.chargers]
        if unknown:
            raise bad(404, f"unknown charger(s) {unknown}")
        return list(dict.fromkeys(ids))

    # Bulk routes first: /chargers/bulk/connect must not match /chargers/{cid}/connect.
    @r.post("/chargers/bulk/connect", summary="Connect several chargers")
    async def bulk_connect(body: Union[List[str], BulkBody] = Body(...)):
        ids = bulk_ids(body)
        return {"ok": True, "changed": {cid: await engine_op(engine.connect, cid) for cid in ids}}

    @r.post("/chargers/bulk/disconnect", summary="Disconnect several chargers")
    async def bulk_disconnect(body: Union[List[str], BulkBody] = Body(...)):
        ids = bulk_ids(body)
        return {"ok": True, "changed": {cid: await engine_op(engine.disconnect, cid) for cid in ids}}

    @r.post("/chargers/{cid}/connect", summary="Start the connection loop")
    async def connect(cid: str):
        runtime(cid)
        return {"ok": True, "changed": await engine_op(engine.connect, cid)}

    @r.post("/chargers/{cid}/disconnect", summary="Close the connection and keep it closed")
    async def disconnect(cid: str):
        runtime(cid)
        return {"ok": True, "changed": await engine_op(engine.disconnect, cid)}

    @r.post("/chargers/{cid}/plug", summary="Plug the EV cable in")
    async def plug(cid: str, body: ConnectorBody = Body(default_factory=ConnectorBody)):
        rt = runtime(cid)
        conn_ok(rt, body.connector)
        await rt.plug(body.connector)
        return {"ok": True}

    @r.post("/chargers/{cid}/unplug", summary="Unplug the EV cable (ends a transaction)")
    async def unplug(cid: str, body: ConnectorBody = Body(default_factory=ConnectorBody)):
        rt = runtime(cid)
        conn_ok(rt, body.connector)
        await rt.unplug(body.connector)
        return {"ok": True}

    @r.post("/chargers/{cid}/swipe", summary="Present an id tag (start / stop like an RFID reader)")
    async def swipe(cid: str, body: TagBody = Body(default_factory=TagBody)):
        rt = runtime(cid)
        conn_ok(rt, body.connector)
        if body.id_tag is None:
            raise bad(400, "id_tag: required")
        status = await rt.swipe(body.connector, body.id_tag)
        return {"ok": True, "status": status.value}

    @r.post("/chargers/{cid}/start", summary="Plug in and present an id tag (default: the first configured)")
    async def start(cid: str, body: TagBody = Body(default_factory=TagBody)):
        rt = runtime(cid)
        conn_ok(rt, body.connector)
        tag = body.id_tag or (rt.cfg.id_tags[0] if rt.cfg.id_tags else None)
        if tag is None:
            raise bad(400, "id_tag: required (the charger has no id_tags configured)")
        if rt.has_transaction(body.connector):
            raise bad(400, f"connector {body.connector} already has a transaction")
        if rt.faulted(body.connector):
            raise bad(400, f"connector {body.connector} is faulted")
        await rt.plug(body.connector)
        status = await rt.swipe(body.connector, tag)
        return {"ok": True, "status": status.value, "id_tag": tag}

    @r.post("/chargers/{cid}/stop", summary="Stop the transaction locally (reason Local)")
    async def stop(cid: str, body: ConnectorBody = Body(default_factory=ConnectorBody)):
        rt = runtime(cid)
        conn_ok(rt, body.connector)
        if not await rt.stop_tx(body.connector):
            raise bad(400, f"connector {body.connector} has no transaction")
        return {"ok": True}

    @r.post("/chargers/{cid}/fault", summary="Fault a connector (StatusNotification Faulted)")
    async def fault(cid: str, body: FaultBody = Body(default_factory=FaultBody)):
        rt = runtime(cid)
        conn_ok(rt, body.connector)
        if body.error_code not in FAULT_CODES:
            raise bad(400, f"error_code: one of {', '.join(FAULT_CODES)}")
        await rt.fault(body.connector, body.error_code)
        return {"ok": True}

    @r.post("/chargers/{cid}/clear-fault", summary="Clear a connector fault")
    async def clear_fault(cid: str, body: ConnectorBody = Body(default_factory=ConnectorBody)):
        rt = runtime(cid)
        conn_ok(rt, body.connector)
        if not await rt.clear_fault(body.connector):
            raise bad(400, f"connector {body.connector} is not faulted")
        return {"ok": True}

    @r.post("/chargers/{cid}/datatransfer", summary="Send a DataTransfer to the CSMS")
    async def datatransfer(cid: str, body: DataTransferBody):
        rt = runtime(cid)
        try:
            res = await rt.send_data_transfer(body.vendor_id, body.message_id, body.data)
        except ConnectionError as e:
            raise bad(400, e) from None
        except Exception as e:  # noqa: BLE001 - CALLERROR / timeout of the CSMS
            raise bad(400, f"DataTransfer failed: {type(e).__name__}: {e}") from None
        return {"ok": res.status is DataTransferStatus.ACCEPTED, "status": res.status.value, "data": res.data}

    @r.get("/chargers/{cid}/profiles", summary="Installed charging profiles")
    async def profiles(cid: str, connector: Optional[int] = Query(None, ge=0)):
        rt = runtime(cid)
        if connector:
            conn_ok(rt, connector)
        return {"profiles": [{"connector_id": ip.connector_id, **jsonable_encoder(ip.profile)}
                             for ip in rt.installed_profiles(connector)]}

    @r.get("/chargers/{cid}/composite", summary="Composite schedule (GetCompositeSchedule, computed locally)")
    async def composite(cid: str, connector: Optional[int] = Query(None, ge=0, description="0 = charge point"),
                        evse: Optional[int] = Query(None, ge=0, description="alias of connector"),
                        duration: int = Query(3600, ge=1, le=7 * 86400), unit: Optional[Literal["W", "A"]] = None):
        rt = runtime(cid)
        cid_ = connector if connector is not None else (evse if evse is not None else 0)
        if cid_:
            conn_ok(rt, cid_)
        res = rt.composite(cid_, duration, ChargingRateUnit(unit) if unit else None)
        if res.schedule is None:
            raise bad(400, f"composite schedule rejected ({res.status.value})")
        return {"connector": cid_, "schedule_start": res.schedule_start, **jsonable_encoder(res.schedule)}

    def frame_filters(charger, action, dir_):
        if dir_ not in (None, "rx", "tx"):
            raise bad(400, "dir: rx or tx")
        return {"charger": charger or None, "action": action or None, "direction": dir_}

    @r.get("/frames", summary="Frame log entries after since (the newest limit of them)")
    async def frames(since: int = Query(0, ge=0), charger: Optional[str] = None, action: Optional[str] = None,
                     dir: Optional[str] = None, limit: int = Query(500, ge=1, le=MAX_FRAMES)):
        f = frame_filters(charger, action, dir)
        rows = engine.frame_log.since(since, limit=limit + 1, **f)
        truncated = len(rows) > limit
        rows = rows[-limit:]
        return {"frames": [e.to_dict() for e in rows], "last_seq": engine.frame_log.last_seq,
                "truncated": truncated}

    @r.get("/frames.jsonl", summary="Frame log as JSON Lines", response_class=Response)
    async def frames_jsonl(since: int = Query(0, ge=0), charger: Optional[str] = None,
                           action: Optional[str] = None, dir: Optional[str] = None):
        rows = engine.frame_log.since(since, **frame_filters(charger, action, dir))
        return Response(engine.frame_log.to_jsonl(rows), media_type=NDJSON,
                        headers={"Content-Disposition": 'attachment; filename="frames.jsonl"'})

    return r


def create_app(engine: OcppEngine, config_path: Optional[str | Path] = None) -> FastAPI:
    app = make_base_app(engine, config_path=config_path, static_dir=STATIC, title="SME OCPPSim",
                        version=__version__, config_model=SimConfig, product_routers=[_router(engine)],
                        devices_attr="chargers", device_key="id", key_type=str,
                        duplicate_hook=duplicate_charger, simulation_hook=simulation_hook)

    async def config_save(request: Request):
        try:
            path = await engine.save_config()
        except ValueError as e:
            raise bad(400, e) from None
        except OSError as e:
            raise bad(400, f"cannot write {engine.config_path}: {e.strerror or e}") from None
        return JSONResponse({"ok": True, "path": path})

    routes = app.router.routes
    for i, rt in enumerate(routes):
        if type(rt) is not Route:
            continue
        methods = sorted((rt.methods or set()) - {"HEAD"})
        if rt.path == "/api/config/save":
            routes[i] = Route(rt.path, config_save, methods=methods, name=rt.name)
        elif rt.path.startswith(("/api/config", "/api/devices")):
            routes[i] = Route(rt.path, _masked(rt.endpoint), methods=methods, name=rt.name)

    async def validation_error(request: Request, exc: RequestValidationError):
        msgs = [f"{'.'.join(str(x) for x in e['loc'][1:]) or e['loc'][0]}: {e['msg']}" for e in exc.errors()]
        return error_response(request, bad(400, "; ".join(msgs)))

    app.add_exception_handler(RequestValidationError, validation_error)
    return app
