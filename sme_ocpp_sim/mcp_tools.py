"""MCP endpoint (``POST /mcp`` on the web port, ``sme_sim_common.mcp_server``, v0.2.2 D).

Every tool wraps one route of :mod:`.web` and is called in-process, so it has the same validation, errors and
side effects as the web UI. The base tools of ``sme_sim_common`` (``get_version``, ``get_state``,
``export_config``, ``set_simulation``) come first; ``get_state`` is the list of chargers with their state.
Like the web UI, the endpoint has no authentication; on a loopback ``--host`` only loopback ``Host`` /
``Origin`` headers are accepted (DNS-rebinding protection of ``/mcp``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from sme_sim_common.mcp_server import McpRegistry, register_base_tools

from . import __version__
from .config import ImpairmentConfig

APP_NAME = "sme-ocpp-sim"
INSTRUCTIONS = ("SME OCPPSim: simulated OCPP 1.6J / 2.0.1 charge points connected to a CSMS. get_state lists the "
                "chargers (id, connection, connectors, transactions, power); every charger tool takes the charger "
                "id as 'cid'. Connector numbers start at 1.")

_CID = {"type": "string", "minLength": 1, "description": "Charger id (chargePointId / station id)"}
_CONNECTOR = {"type": "integer", "minimum": 1, "default": 1, "description": "Connector number (1 = first)"}
_ID_TAG = {"type": "string", "minLength": 1, "maxLength": 20,
           "description": "Id tag (RFID token) to present; default: the charger's first configured id tag"}


def _obj(props: Dict[str, Any], required: Optional[List[str]] = None, *, cid: bool = True) -> Dict[str, Any]:
    """An ``object`` input schema; ``cid`` (required) first unless ``cid=False``. No other arguments allowed."""
    properties = {"cid": _CID, **props} if cid else dict(props)
    req = (["cid"] if cid else []) + list(required or [])
    out: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if req:
        out["required"] = req
    return out


def _model_props(model: type[BaseModel]) -> Dict[str, Any]:
    """The properties of a flat pydantic body model (no ``$defs``), ``title`` keys dropped."""
    schema = model.model_json_schema()
    return {k: {kk: vv for kk, vv in v.items() if kk != "title"} for k, v in schema["properties"].items()}


def build_registry(app: Any) -> McpRegistry:
    """The OCPPSim MCP tools, attached to ``app`` (not mounted yet)."""
    mcp = McpRegistry(APP_NAME, version=__version__, instructions=INSTRUCTIONS)
    register_base_tools(mcp, app)
    add = mcp.register_http_tool
    conn = _obj({"connector": _CONNECTOR})

    # ---- read
    add("get_charger", "One charger: its snapshot (connection, connectors, transactions, meter) plus the fault "
        "codes and CSMS -> charger actions of its OCPP version.", "GET", "/api/chargers/{cid}", _obj({}))
    add("get_charger_settings", "Connection / authorization settings of a charger (CSMS URL, OCPP version, id "
        "tags ...; the password is masked) and the effective CSMS URL / version.",
        "GET", "/api/chargers/{cid}/settings", _obj({}))
    add("get_charging_profiles", "Charging profiles installed on a charger by the CSMS (Smart Charging).",
        "GET", "/api/chargers/{cid}/profiles",
        _obj({"connector": {"type": "integer", "minimum": 0, "description": "Only this connector (0 = charger)"}}))
    add("get_composite_schedule", "Composite charging schedule of a connector, computed locally like "
        "GetCompositeSchedule.", "GET", "/api/chargers/{cid}/composite",
        _obj({"connector": {"type": "integer", "minimum": 0, "default": 0, "description": "0 = whole charger"},
              "duration": {"type": "integer", "minimum": 1, "maximum": 604800, "default": 3600,
                           "description": "Seconds"},
              "unit": {"type": "string", "enum": ["W", "A"], "description": "Rate unit (default: the profiles')"}}))
    add("get_frames", "OCPP frame log entries (oldest first) with seq > since; filters: charger, action, dir "
        "(rx = received from the CSMS, tx = sent). Poll with since = the last seq you have.", "GET", "/api/frames",
        _obj({"since": {"type": "integer", "minimum": 0, "default": 0},
              "charger": {"type": "string", "description": "Charger id"},
              "action": {"type": "string", "description": "OCPP action, e.g. BootNotification"},
              "dir": {"type": "string", "enum": ["rx", "tx"]},
              "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 100}}, cid=False))
    add("get_impairments", "Fault injection settings of a charger: command blocking, latency / jitter, periodic "
        "drops.", "GET", "/api/chargers/{cid}/impairments", _obj({}))

    # ---- connection
    add("connect_charger", "Start the charger's connection loop to its CSMS (BootNotification follows).",
        "POST", "/api/chargers/{cid}/connect", _obj({}), destructive=False)
    add("disconnect_charger", "Close the charger's connection to the CSMS and keep it closed until "
        "connect_charger.", "POST", "/api/chargers/{cid}/disconnect", _obj({}), destructive=False)

    # ---- EV / transactions
    add("plug_in", "Plug the EV cable into a connector (status Preparing / Occupied).",
        "POST", "/api/chargers/{cid}/plug", conn, destructive=False)
    add("unplug", "Unplug the EV cable from a connector; a running transaction ends.",
        "POST", "/api/chargers/{cid}/unplug", conn, destructive=False)
    add("authorize", "Present an id tag at a connector like an RFID reader: authorizes with the CSMS and starts "
        "(or, with the same tag, stops) a transaction when the cable is plugged in.",
        "POST", "/api/chargers/{cid}/swipe", _obj({"connector": _CONNECTOR, "id_tag": {**_ID_TAG,
                                                    "description": "Id tag (RFID token) to present"}}, ["id_tag"]),
        destructive=False)
    add("start_transaction", "Plug in and present an id tag in one step (the CSMS decides whether charging "
        "starts). Fails when the connector already has a transaction or is faulted.",
        "POST", "/api/chargers/{cid}/start", _obj({"connector": _CONNECTOR, "id_tag": _ID_TAG}), destructive=False)
    add("stop_transaction", "Stop the connector's transaction locally (reason Local).",
        "POST", "/api/chargers/{cid}/stop", conn, destructive=False)

    # ---- faults / limits
    fault_codes = _model_props(_fault_body())["error_code"]
    add("set_fault", "Fault a connector (StatusNotification Faulted with the error code).", "POST", "/api/chargers/{cid}/fault",
        _obj({"connector": _CONNECTOR, "error_code": {**fault_codes, "description": "Fault code of the charger's "
                                                      "OCPP version (get_charger lists them)"}}), destructive=False)
    add("clear_fault", "Clear a connector fault (back to Available).",
        "POST", "/api/chargers/{cid}/clear-fault", conn, destructive=False)
    add("set_external_limit", "OCPP 2.0.1 only: set (limit_w) or remove (no limit_w) an external charging limit, "
        "e.g. from a local EMS; the station reports it to the CSMS.", "POST", "/api/chargers/{cid}/external-limit",
        _obj({"connector": {"type": "integer", "minimum": 0, "default": 0, "description": "0 = whole charger"},
              "limit_w": {"type": "number", "minimum": 0, "description": "Limit in W; omit to remove the limit"}}),
        destructive=False)

    # ---- fault injection (expert)
    add("set_impairments", "Replace a charger's impairments block (applied live, no reconnect): block (CSMS -> "
        "charger action -> NotImplemented / NotSupported / NoReply), latency_ms / jitter_ms / direction / seed, "
        "drop_every_s / drop_jitter_s / drop_mode / down_s. Omitted fields get their defaults (off).",
        "PUT", "/api/chargers/{cid}/impairments", _obj(_model_props(ImpairmentConfig)), destructive=True)
    add("drop_connection", "Drop the charger's connection now (simulated network failure); it reconnects after "
        "reconnect_after_s (default: impairments.down_s). Transactions are kept.",
        "POST", "/api/chargers/{cid}/drop",
        _obj({"mode": {"type": "string", "enum": ["abort", "close"],
                       "description": "abort = no close frame (pulled cable), close = close code 1001"},
              "reconnect_after_s": {"type": "number", "minimum": 0, "maximum": 86400}}), destructive=False)
    add("send_frame", "Expert use: send an arbitrary charger -> CSMS CALL (action + payload, checked against the "
        "OCPP schema unless validate is false) and return the CSMS answer, or write a raw frame verbatim. The "
        "simulated station's state does not change.", "POST", "/api/chargers/{cid}/send",
        _obj({"action": {"type": "string", "minLength": 1, "description": "OCPP action, e.g. Heartbeat"},
              "payload": {"description": "CALL payload (JSON object)", "default": {}},
              "validate": {"type": "boolean", "default": True},
              "raw": {"type": "string", "minLength": 1, "description": "A raw frame text instead of action"},
              "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 60}}), destructive=True)

    # ---- built-in Mini CSMS
    add("mini_csms_status", "Built-in Mini CSMS: status and the stations connected to it (error when "
        "mini_csms.enabled is off).", "GET", "/api/csms", None)
    add("mini_csms_call", "Built-in Mini CSMS: send a CSMS -> charger CALL (e.g. RemoteStartTransaction, "
        "SetChargingProfile) to a connected station and return its answer.", "POST", "/api/csms/chargers/{cid}/call",
        _obj({"action": {"type": "string", "minLength": 1}, "payload": {"type": "object", "default": {}},
              "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 300}}, ["action"]), destructive=True)
    return mcp


def _fault_body() -> type[BaseModel]:
    from .web import FaultBody

    return FaultBody


def mount(app: Any, host: Optional[str] = None) -> McpRegistry:
    """Register the tools and serve them at ``POST /mcp`` on ``app`` (call before the app starts)."""
    mcp = build_registry(app)
    mcp.mount(app, host=host)
    return mcp
