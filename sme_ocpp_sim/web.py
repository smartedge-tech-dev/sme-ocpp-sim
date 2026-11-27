"""FastAPI app (plan §5 M6).

The generic routes (``/``, ``/api/version``, ``/api/state``, ``/api/config*``, ``/api/devices*``,
``/api/simulation``, ``/ws``, the ``/sme-sim-common`` and ``/static`` mounts) come from
``sme_sim_common.web.make_base_app`` (devices = ``chargers``, key = ``id``). This module adds the OCPP routes:

* ``GET /api/chargers/{id}`` - the charger's snapshot plus ``fault_codes``, the fault codes of its OCPP version;
* ``POST /api/chargers/{id}/{connect,disconnect}`` - start / stop the connection loop (engine level; a disconnect
  is sticky until ``connect``, transactions survive it like a network loss);
* ``POST /api/chargers/{id}/{plug,unplug,swipe,start,stop,fault,clear-fault}`` - body ``{"connector": 1, ...}``
  (``id_tag`` for swipe / start, ``error_code`` for fault: one of the charger's ``fault_codes`` (1.6:
  ``ChargePointErrorCode``), default ``OtherError``; a fault ends a running transaction with reason ``Other``
  and reports ``Faulted``, clear-fault returns the connector to Available / Preparing);
* ``POST /api/chargers/{id}/datatransfer`` - body ``{"vendor_id", "message_id", "data"}``, CP -> CSMS;
* ``GET /api/chargers/{id}/profiles?connector=`` - installed charging profiles;
* ``GET /api/chargers/{id}/composite?connector=&duration=&unit=`` - the GetCompositeSchedule answer computed
  locally. In OCPP 1.6 the plan's ``evse`` is a connectorId, so the parameter is ``connector`` (``evse`` is
  accepted as an alias; 0 = the whole charge point); for 2.0.1 ``connector`` is the EVSE id;
* ``POST /api/chargers/{id}/external-limit`` - body ``{"connector": 0, "limit_w": 7000}`` (``limit_w`` ``null`` =
  remove): an external (energy management) limit. 2.0.1 installs a ChargingStationExternalConstraints profile
  and sends NotifyChargingLimit / ClearedChargingLimit; a version without external limits (1.6) answers 400;
* ``GET /api/frames?since=&until=&charger=&action=&dir=&limit=`` / ``GET /api/frames.jsonl?<same>`` - the frame
  log (the JSON Lines export is streamed; without ``limit`` it holds every matching entry in the buffer; ``until``
  = only entries with ``seq <= until``, so ``since=n-1&until=n`` is the exact entry ``n``);
* ``GET|PUT /api/csms-defaults`` - the ``csms_defaults`` block as JSON (PUT replaces the whole block: an omitted
  field is its model default, so a client sends back what GET returned with its changes);
* ``GET|PATCH /api/chargers/{id}/settings`` - the connection / authorization subset of a charger's config
  (:data:`SETTINGS_FIELDS`, the password masked). PATCH changes only the fields it contains (``null`` = unset;
  the mask keeps the stored password) and leaves every other field of the charger as it is;
* ``GET /api/chargers/{id}/configuration`` - the run-time configuration keys (what the CSMS sees with
  GetConfiguration, incl. values it changed with ChangeConfiguration and the ``readonly`` flags). A 2.0.1
  charger has no keys (``keys`` is empty) and adds ``ocpp_version`` and ``variables``: its Device Model, one
  entry per variable with its ``attributes`` (``Actual`` / ``Target`` / ``MinSet`` / ``MaxSet`` values;
  ``WriteOnly`` values are ``null``), incl. values the CSMS changed with SetVariables.

The JSON config routes validate with the pydantic models (the whole :class:`SimConfig` again, so cross-field rules
such as the security profile apply) and go through ``engine.reload`` (the engine lock; a changed charger restarts).
Like the YAML routes they read the current config before taking the lock, so two concurrent edits can overwrite
each other. Nothing is written to the file until ``POST /api/config/save`` (block write-back, comments outside the
changed blocks kept, values equal to their defaults not written);
* ``POST /api/chargers/bulk/{connect,disconnect}`` - body ``{"ids": [...]}`` / ``["A", "B"]`` / ``{"all": true}``
  (``ids`` and ``all`` together are rejected). Not atomic: the chargers are handled one after the other and an
  error stops the request with the earlier ones already changed. ``bulk`` is not a valid charger id
  (``config.py``), so the single routes never clash with these.

Errors: an unknown charger is 404, invalid input or an operation the current state does not allow is 400 (both
``sme_sim_common.web.bad``; request validation errors are 400 too instead of FastAPI's 422). A DataTransfer the
CSMS does not answer in time is 504, one it answers with CALLERROR (or garbage) is 502. A config file that cannot
be written by ``POST /api/config/save`` is 500.

Secrets: ``/api/state`` and the frame log never contain passwords. The frame log holds OCPP frames only (no HTTP
headers); the ``Authorization`` header of the handshake is redacted from the ``websockets`` logger in
``rpc/transport.py``. The base config / device routes are wrapped so that every ``password`` they return is
:data:`PASSWORD_MASK` (:func:`mask_text` never returns a clear password); a config sent back with the mask keeps
the stored password (``OcppEngine.reload``), and a ``PUT /api/devices/{id}/config`` that renames a charger keeps
the password of ``{id}``. Device / config PUTs replace the whole item: an omitted ``password`` means no password.
``POST /api/config/save`` writes through ``config.save_file`` (comments kept) in a worker thread under the engine
lock (``OcppEngine.save_config``).

``/ws`` client contract: the first message is ``{"type": "snapshot", "data": <GET /api/state>}``. Then the
engine's pushes follow (``charger_state`` / ``values`` / ``frame``, see ``engine.py``), each with ``push_seq``
counting 1, 2, 3 ... per connection, ``{"type": "log", "event": {...}}`` engine events (numbered by their own
``event.seq``, no ``push_seq``) and ``{"type": "snapshot"}`` again after a config reload. A gap in
``push_seq`` means messages were dropped for a slow client: if a state push or a reload notice was among them,
the next state push is a full one (``full: true``) so state heals by itself (or the client refetches
``/api/state``); lost frames are fetched with ``GET /api/frames?since=<last frame seq seen>``. A full
``charger_state`` lists *every* charger: the client replaces its charger list with it (a charger missing from it
was removed by a reload); a full ``values`` replaces all values likewise. Clients must not rely on the order of
the two full pushes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import yaml
from fastapi import APIRouter, Body, FastAPI, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from sme_sim_common.web import bad, error_response, make_base_app
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from . import __version__
from .config import PASSWORD_MASK, CsmsDefaults, SimConfig
from .engine import OcppEngine, OperationNotPossible
from .protocol.base import DataTransferStatus
from .protocol.registry import get_station_class, list_versions
from .rpc.errors import CallError

STATIC = Path(__file__).resolve().parent / "static"
#: Fault codes an operator may set on some charger: the union over the available OCPP versions (each version's
#: station provides its own; 1.6 = ``ChargePointErrorCode`` without ``NoError``). ``POST .../fault`` also checks
#: the charger's own list, which ``GET /api/chargers/{id}`` returns.
FAULT_CODES = tuple(dict.fromkeys(code for v in list_versions() if v.available
                                  for code in get_station_class(v.version).fault_codes))
#: Literal type of :data:`FAULT_CODES` (an enum in the OpenAPI schema).
FaultCode = Literal[FAULT_CODES]  # type: ignore[valid-type]
MAX_FRAMES = 5000
NDJSON = "application/x-ndjson"
JSONL_CHUNK = 500  # frame log entries per streamed chunk
#: What :func:`mask_text` returns for text that is not valid YAML (it cannot be masked reliably).
#: Charger config fields of ``/api/chargers/{id}/settings``.
SETTINGS_FIELDS = ("csms_url", "ocpp_version", "password", "ca_file", "client_cert_file", "client_key_file",
                   "verify_tls", "id_tags", "config_keys")
UNPARSABLE_PLACEHOLDER = "# configuration not shown: it is not valid YAML, so its passwords cannot be masked\n"


# ------------------------------------------------------------------ request bodies

class ConnectorBody(BaseModel):
    connector: int = Field(1, ge=1)


class TagBody(ConnectorBody):
    id_tag: Optional[str] = Field(None, min_length=1, max_length=20)


class FaultBody(ConnectorBody):
    error_code: FaultCode = "OtherError"


class ExternalLimitBody(BaseModel):
    connector: int = Field(0, ge=0)  # 0 = the whole charger
    limit_w: Optional[float] = Field(None, ge=0)  # None = remove the limit


class DataTransferBody(BaseModel):
    vendor_id: str = Field(min_length=1, max_length=255)
    message_id: Optional[str] = Field(None, max_length=50)
    data: Optional[str] = None


class ChargerSettingsBody(BaseModel):
    """PATCH body of ``/api/chargers/{id}/settings``: only the fields present are changed (``null`` = unset)."""
    model_config = ConfigDict(extra="forbid")
    csms_url: Optional[str] = None
    ocpp_version: Optional[str] = None
    password: Optional[str] = None
    ca_file: Optional[str] = None
    client_cert_file: Optional[str] = None
    client_key_file: Optional[str] = None
    verify_tls: Optional[bool] = None
    id_tags: Optional[List[str]] = None
    config_keys: Optional[Dict[str, Union[str, int, float, bool]]] = None


class BulkBody(BaseModel):
    ids: List[str] = Field(default_factory=list)
    all: bool = False


# ------------------------------------------------------------------ password masking

_PASSWORD_LINE = re.compile(r"(?m)^([ \t]*(?:-[ \t]+)?password[ \t]*:)[ \t]*(?!(?:null|Null|NULL|~)?[ \t]*(?:#.*)?$)\S.*$")


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


def _secrets(obj: Any) -> List[str]:
    """The clear (non-null, non-mask) ``password`` values in ``obj`` as text."""
    if isinstance(obj, dict):
        return [s for k, v in obj.items()
                for s in ([str(v)] if k == "password" and v not in (None, PASSWORD_MASK) else _secrets(v))]
    if isinstance(obj, list):
        return [s for v in obj for s in _secrets(v)]
    return []


def _single_line_passwords(node: Any) -> bool:
    """Every ``password`` value in the composed YAML ``node`` is a one-line scalar (no block / multi-line scalar,
    no collection), i.e. something the line rewrite of :func:`mask_text` replaces completely."""
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode) and k.value == "password":
                if (not isinstance(v, yaml.ScalarNode) or v.style in ("|", ">")
                        or v.start_mark.line != v.end_mark.line):
                    return False
            elif not _single_line_passwords(v):
                return False
        return True
    if isinstance(node, yaml.SequenceNode):
        return all(_single_line_passwords(v) for v in node.value)
    return True


def mask_text(text: str) -> str:
    """YAML ``text`` with every password masked; never returns a clear password.

    One-line block-style ``password:`` values are rewritten in place (the rest of the line, including a trailing
    comment, is dropped) so the other comments survive. That result is used only if every password value was such
    a one-line scalar, it parses without a clear password and no clear password occurs in it as text; otherwise
    (block / multi-line scalars, flow style, quoted keys, aliases ...) the document is re-dumped from the masked
    data (comments lost). Text that is not valid YAML gives :data:`UNPARSABLE_PLACEHOLDER`, never the raw text."""
    try:
        data = yaml.safe_load(text)
        node = yaml.compose(text)
    except yaml.YAMLError:
        return UNPARSABLE_PLACEHOLDER
    secrets = _secrets(data)
    if not secrets:
        return text
    out = _PASSWORD_LINE.sub(lambda m: f'{m.group(1)} "{PASSWORD_MASK}"', text)
    try:
        ok = (_single_line_passwords(node) and not _unmasked(yaml.safe_load(out))
              and not any(s in out for s in secrets))
    except yaml.YAMLError:
        ok = False
    return out if ok else yaml.safe_dump(mask_obj(data), allow_unicode=True, sort_keys=False)


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


def _device_put(endpoint, engine: OcppEngine):
    """``PUT /api/devices/{id}/config``: a renamed charger whose password is the mask keeps the password of
    ``{id}`` (the engine's own unmasking matches by the *new* id). The base route reads the current config outside
    the engine lock as well, so this lookup is no less consistent than the rest of that read-modify-write."""
    async def wrapper(request: Request):
        body = await request.body()
        try:
            data = yaml.safe_load(body.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            data = None  # the base route reports it
        old_id = request.path_params.get("device")
        if (isinstance(data, dict) and data.get("password") == PASSWORD_MASK and data.get("id") is not None
                and str(data["id"]) != old_id):
            old = next((c.password for c in engine.config.chargers if c.id == old_id), None)
            if old is not None:
                body = yaml.safe_dump({**data, "password": old}, allow_unicode=True, sort_keys=False).encode("utf-8")
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return await request.receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await endpoint(Request(request.scope, receive))
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
        except KeyError:
            raise bad(404, f"unknown charger {cid!r}") from None
        except OperationNotPossible as e:
            raise bad(400, e) from None

    def bulk_ids(body: Union[List[str], BulkBody]) -> List[str]:
        if isinstance(body, BulkBody):
            if body.all and body.ids:
                raise bad(400, "give either ids or all: true, not both")
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

    @r.get("/chargers/{cid}", summary="One charger: its snapshot plus the fault codes of its OCPP version")
    async def charger(cid: str):
        try:
            return engine.charger_detail(cid)
        except KeyError:
            raise bad(404, f"unknown charger {cid!r}") from None

    @r.post("/chargers/{cid}/connect", summary="Start the connection loop")
    async def connect(cid: str):
        return {"ok": True, "changed": await engine_op(engine.connect, cid)}

    @r.post("/chargers/{cid}/disconnect", summary="Close the connection and keep it closed")
    async def disconnect(cid: str):
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
        """Check-then-act: the transaction / fault checks and the plug + swipe that follow are not atomic (a CSMS
        RemoteStart or another request may run in between); the runtime then answers by its own rules, the checks
        only give the common mistakes a clear 400."""
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
        if body.error_code not in rt.fault_codes:
            raise bad(400, f"error_code {body.error_code!r} is not a fault code of OCPP {rt.adapter.version}")
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
        except ConnectionError as e:  # not connected: a state the operator can change
            raise bad(400, e) from None
        except TimeoutError:
            raise bad(504, "DataTransfer: the CSMS did not answer in time") from None
        except CallError as e:
            raise bad(502, f"DataTransfer: the CSMS answered CALLERROR {e}") from None
        except Exception as e:  # noqa: BLE001 - unparsable answer and other CSMS-side failures
            raise bad(502, f"DataTransfer failed: {type(e).__name__}: {e}") from None
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
        if connector is not None and evse is not None and connector != evse:
            raise bad(400, f"connector={connector} and evse={evse} differ (evse is an alias of connector)")
        cid_ = connector if connector is not None else (evse if evse is not None else 0)
        if cid_:
            conn_ok(rt, cid_)
        res = rt.composite(cid_, duration, unit)
        if res.schedule is None:
            raise bad(400, f"composite schedule rejected ({res.status.value})")
        return {"connector": cid_, "schedule_start": res.schedule_start, **jsonable_encoder(res.schedule)}

    @r.post("/chargers/{cid}/external-limit", summary="Set or remove an external charging limit (OCPP 2.0.1)")
    async def external_limit(cid: str, body: ExternalLimitBody = Body(default_factory=ExternalLimitBody)):
        rt = runtime(cid)
        try:  # the station checks the connector, after the version (1.6: not supported)
            await rt.set_external_limit(body.connector, body.limit_w)
        except NotImplementedError:
            raise bad(400, f"external limits are not supported by OCPP {rt.adapter.version}") from None
        except ValueError as e:
            raise bad(400, e) from None
        return {"ok": True}

    async def apply(data: dict) -> None:
        """Validate ``data`` (a whole config dump) and reload the engine with it; any rejection is a 400."""
        try:
            cfg = SimConfig.model_validate(data)
        except ValueError as e:  # pydantic ValidationError
            raise bad(400, e) from None
        try:
            await engine.reload(cfg)
        except Exception as e:  # noqa: BLE001 - build / apply errors: the old config keeps running
            raise bad(400, e) from None

    def charger_cfg(cid: str):
        for i, c in enumerate(engine.config.chargers):
            if c.id == cid:
                return i, c
        raise bad(404, f"unknown charger {cid!r}")

    def settings_view(c) -> dict:
        out = {k: getattr(c, k) for k in SETTINGS_FIELDS}
        if out["password"] is not None:
            out["password"] = PASSWORD_MASK
        url, version = engine.config.resolve_csms(c)
        return {"id": c.id, **jsonable_encoder(out), "effective": {"csms_url": url, "ocpp_version": version}}

    @r.get("/csms-defaults", summary="The csms_defaults block")
    async def csms_defaults_get():
        return engine.config.csms_defaults.model_dump(mode="json")

    @r.put("/csms-defaults", summary="Replace the csms_defaults block (reload)")
    async def csms_defaults_put(body: CsmsDefaults):
        data = engine.config.model_dump()
        data["csms_defaults"] = body.model_dump()
        await apply(data)
        return {"ok": True, **engine.config.csms_defaults.model_dump(mode="json")}

    @r.get("/chargers/{cid}/settings", summary="Connection / authorization settings of a charger")
    async def settings_get(cid: str):
        return settings_view(charger_cfg(cid)[1])

    @r.patch("/chargers/{cid}/settings", summary="Change some settings of a charger (reload)")
    async def settings_patch(cid: str, body: ChargerSettingsBody = Body(default_factory=ChargerSettingsBody)):
        i, c = charger_cfg(cid)
        changes = body.model_dump(include=body.model_fields_set)
        if changes.get("password") == PASSWORD_MASK:
            del changes["password"]  # the mask keeps the stored password
        if "verify_tls" in changes and changes["verify_tls"] is None:
            raise bad(400, "verify_tls: true or false")
        for k, empty in (("id_tags", []), ("config_keys", {})):
            if k in changes and changes[k] is None:
                changes[k] = empty
        if changes:
            data = engine.config.model_dump()
            data["chargers"][i] = {**c.model_dump(), **changes}
            await apply(data)
        return {"ok": True, **settings_view(charger_cfg(cid)[1])}

    @r.get("/chargers/{cid}/configuration", summary="Run-time configuration keys (as GetConfiguration sees them)")
    async def configuration(cid: str):
        rt = runtime(cid)
        out: Dict[str, Any] = {"keys": [{"key": e.key, "readonly": e.readonly, "value": e.value}
                                        for e in rt.configuration()]}
        variables = rt.variables()
        if variables is not None:  # a Device Model version (2.0.1): tagged, ``keys`` stays empty
            out.update(ocpp_version=rt.adapter.version, variables=variables)
        return out

    def frame_filters(charger, action, dir_):
        if dir_ not in (None, "rx", "tx"):
            raise bad(400, "dir: rx or tx")
        return {"charger": charger or None, "action": action or None, "direction": dir_}

    @r.get("/frames", summary="Frame log entries after since (the newest limit of them)")
    async def frames(since: int = Query(0, ge=0), until: Optional[int] = Query(None, ge=0, description="seq <= until"),
                     charger: Optional[str] = None, action: Optional[str] = None,
                     direction: Optional[str] = Query(None, alias="dir", description="rx or tx"),
                     limit: int = Query(500, ge=1, le=MAX_FRAMES)):
        """Matching entries with ``seq > since``, oldest first; at most the newest ``limit``. ``truncated`` means
        older matching entries after ``since`` were left out: fetch them with a larger ``limit`` (up to
        :data:`MAX_FRAMES`) or from ``/api/frames.jsonl`` (no limit by default). A client polling for new entries
        passes the ``seq`` of the last entry it has as ``since``; ``last_seq`` is the newest seq of the whole log
        (filters ignored)."""
        f = frame_filters(charger, action, direction)
        if until is None:
            rows = engine.frame_log.since(since, limit=limit + 1, **f)
        else:
            rows = [e for e in engine.frame_log.since(since, **f) if e.seq <= until]
        truncated = len(rows) > limit
        rows = rows[-limit:]
        return {"frames": [e.to_dict() for e in rows], "last_seq": engine.frame_log.last_seq,
                "truncated": truncated}

    @r.get("/frames.jsonl", summary="Frame log as JSON Lines (streamed)", response_class=StreamingResponse)
    async def frames_jsonl(since: int = Query(0, ge=0), charger: Optional[str] = None,
                           action: Optional[str] = None,
                           direction: Optional[str] = Query(None, alias="dir", description="rx or tx"),
                           limit: Optional[int] = Query(None, ge=1, description="only the newest limit entries")):
        """Same selection as ``/api/frames`` (without a default limit), one JSON object per line. The entries are
        picked when the request arrives and streamed in chunks of :data:`JSONL_CHUNK`."""
        rows = engine.frame_log.since(since, limit=limit, **frame_filters(charger, action, direction))

        def chunks():
            for i in range(0, len(rows), JSONL_CHUNK):
                yield engine.frame_log.to_jsonl(rows[i:i + JSONL_CHUNK])

        return StreamingResponse(chunks(), media_type=NDJSON,
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
            raise bad(500, f"cannot write {engine.config_path}: {e.strerror or e}") from None
        return JSONResponse({"ok": True, "path": path})

    routes = app.router.routes
    for i, rt in enumerate(routes):
        if type(rt) is not Route:
            continue
        methods = sorted((rt.methods or set()) - {"HEAD"})
        if rt.path == "/api/config/save":
            routes[i] = Route(rt.path, config_save, methods=methods, name=rt.name)
        elif rt.path == "/api/devices/{device}/config" and "PUT" in methods:
            routes[i] = Route(rt.path, _masked(_device_put(rt.endpoint, engine)), methods=methods, name=rt.name)
        elif rt.path.startswith(("/api/config", "/api/devices")):
            routes[i] = Route(rt.path, _masked(rt.endpoint), methods=methods, name=rt.name)

    async def validation_error(request: Request, exc: RequestValidationError):
        msgs = [f"{'.'.join(str(x) for x in e['loc'][1:]) or e['loc'][0]}: {e['msg']}" for e in exc.errors()]
        return error_response(request, bad(400, "; ".join(msgs)))

    app.add_exception_handler(RequestValidationError, validation_error)
    return app
