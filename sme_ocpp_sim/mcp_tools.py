"""MCP endpoint (``POST /mcp`` on the web port, ``sme_sim_common.mcp_server``, v0.2.2 D).

Every tool wraps one route of :mod:`.web` and is called in-process, so it has the same validation, errors and
side effects as the web UI. The base tools of ``sme_sim_common`` (``get_version``, ``get_state``,
``export_config``, ``set_simulation``) come first; ``get_state`` is the list of chargers with their state.
Like the web UI, the endpoint has no authentication; on a loopback ``--host`` only loopback ``Host`` /
``Origin`` headers are accepted (DNS-rebinding protection of ``/mcp`` only).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from sme_sim_common.mcp_server import McpRegistry, register_base_tools

from . import __version__
from .config import ImpairmentConfig, TrafficConfig
from .web import MAX_FRAMES, DataTransferBody, ExpectFrameBody, FaultBody

APP_NAME = "sme-ocpp-sim"
INSTRUCTIONS = ("SME OCPPSim: simulated OCPP 1.6J / 2.0.1 / 2.1 charge points connected to a CSMS. get_state lists the "
                "chargers (id, connection, connectors, transactions, power); every charger tool takes the charger "
                "id as 'cid'. Connector numbers start at 1.")

_CID = {"type": "string", "minLength": 1, "description": "Charger id (chargePointId / station id)"}
_CONNECTOR = {"type": "integer", "minimum": 1, "default": 1, "description": "Connector number (1 = first)"}
_ID_TAG_DEFAULTED = {"type": "string", "minLength": 1, "maxLength": 20,
                     "description": "Id tag (RFID token) to present; default: the charger's first configured id tag"}
_ID_TAG_REQUIRED = {"type": "string", "minLength": 1, "maxLength": 20, "description": "Id tag (RFID token) to present"}
_WHOLE_CHARGER = {"type": "integer", "minimum": 0, "default": 0, "description": "0 = whole charger"}


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
    defs = schema.get("$defs", {})

    def clean(v: Any, seen: frozenset = frozenset()) -> Any:
        if isinstance(v, dict):
            out = {kk: clean(vv, seen) for kk, vv in v.items() if kk not in ("title", "$ref")}
            if "$ref" in v:  # a nested model: inlined (MCP clients do not all resolve $defs)
                name = v["$ref"].rsplit("/", 1)[-1]
                if name in seen:
                    raise ValueError(f"recursive model {name} cannot be inlined")
                # The sibling keys (default, description) of the reference win over the model's own.
                out = {**clean(defs[name], seen | {name}), **out}
            return out
        if isinstance(v, list):
            return [clean(x, seen) for x in v]
        return v

    return {k: clean(v) for k, v in schema["properties"].items()}


def build_registry(app: Any) -> McpRegistry:
    """The OCPPSim MCP tools, attached to ``app`` (not mounted yet)."""
    mcp = McpRegistry(APP_NAME, version=__version__, instructions=INSTRUCTIONS)
    register_base_tools(mcp, app)
    add = mcp.register_http_tool
    conn = _obj({"connector": _CONNECTOR})
    no_args = _obj({}, cid=False)

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
        _obj({"connector": _WHOLE_CHARGER,
              "duration": {"type": "integer", "minimum": 1, "maximum": 604800, "default": 3600,
                           "description": "Seconds"},
              "unit": {"type": "string", "enum": ["W", "A"], "description": "Rate unit (default: the profiles')"}}))
    add("get_frames", "OCPP frame log entries (oldest first) with seq > since; filters: charger, action, dir "
        "(rx = received from the CSMS, tx = sent). Poll with since = the last seq you have.", "GET", "/api/frames",
        _obj({"since": {"type": "integer", "minimum": 0, "default": 0},
              "charger": {"type": "string", "description": "Charger id"},
              "action": {"type": "string", "description": "OCPP action, e.g. BootNotification"},
              "dir": {"type": "string", "enum": ["rx", "tx"]},
              "limit": {"type": "integer", "minimum": 1, "maximum": MAX_FRAMES, "default": 500}}, cid=False))
    add("get_impairments", "Fault injection settings of a charger: command blocking, latency / jitter, periodic "
        "drops, remote start / meter value / slow response anomalies.", "GET", "/api/chargers/{cid}/impairments", _obj({}))

    # ---- connection
    add("connect_charger", "Start the charger's connection loop to its CSMS (BootNotification follows).",
        "POST", "/api/chargers/{cid}/connect", _obj({}), destructive=False)
    add("disconnect_charger", "Close the charger's connection to the CSMS and keep it closed until "
        "connect_charger.", "POST", "/api/chargers/{cid}/disconnect", _obj({}), destructive=True)

    # ---- EV / transactions
    add("plug_in", "Plug the EV cable into a connector (status Preparing / Occupied).",
        "POST", "/api/chargers/{cid}/plug", conn, destructive=False)
    add("unplug", "Unplug the EV cable from a connector; a running transaction ends.",
        "POST", "/api/chargers/{cid}/unplug", conn, destructive=False)
    add("authorize", "Present an id tag at a connector like an RFID reader: authorizes with the CSMS and starts "
        "(or, with the same tag, stops) a transaction when the cable is plugged in.",
        "POST", "/api/chargers/{cid}/swipe", _obj({"connector": _CONNECTOR, "id_tag": _ID_TAG_REQUIRED}, ["id_tag"]),
        destructive=False)
    add("start_transaction", "Plug in and present an id tag in one step (the CSMS decides whether charging "
        "starts). Fails when the connector already has a transaction or is faulted.",
        "POST", "/api/chargers/{cid}/start", _obj({"connector": _CONNECTOR, "id_tag": _ID_TAG_DEFAULTED}),
        destructive=False)
    add("stop_transaction", "Stop the connector's transaction locally (reason Local).",
        "POST", "/api/chargers/{cid}/stop", conn, destructive=False)
    add("data_transfer", "Send a DataTransfer CALL from the charger to the CSMS and return its answer "
        "(status, data).", "POST", "/api/chargers/{cid}/datatransfer",
        _obj(_model_props(DataTransferBody), ["vendor_id"]), destructive=False)

    # ---- faults / limits
    error_code = {**_model_props(FaultBody)["error_code"],
                  "description": "Fault code of the charger's OCPP version (get_charger lists them)"}
    add("set_fault", "Fault a connector (StatusNotification Faulted with the error code).",
        "POST", "/api/chargers/{cid}/fault", _obj({"connector": _CONNECTOR, "error_code": error_code}),
        destructive=False)
    add("clear_fault", "Clear a connector fault (back to Available).",
        "POST", "/api/chargers/{cid}/clear-fault", conn, destructive=False)
    add("set_external_limit", "OCPP 2.0.1 / 2.1 only (not 1.6): set (limit_w) or remove (no limit_w) an external charging limit, "
        "e.g. from a local EMS; the station reports it to the CSMS.", "POST", "/api/chargers/{cid}/external-limit",
        _obj({"connector": _WHOLE_CHARGER,
              "limit_w": {"type": "number", "minimum": 0, "description": "Limit in W; omit to remove the limit"}}),
        destructive=False)

    # ---- fault injection (expert)
    add("set_impairments", "Replace a charger's impairments block (applied live, no reconnect): block (CSMS -> "
        "charger action -> NotImplemented / NotSupported / NoReply), latency_ms / jitter_ms / direction / seed, "
        "drop_every_s / drop_jitter_s / drop_mode / down_s, remote_start (accept_no_tx: Accepted but no "
        "transaction), meter (drop_ratio / reorder_window / end_after_stop / voltage_noise_v), response_delay_ms "
        "(CSMS -> charger action -> ms the answer is held). Omitted fields get their defaults (off).",
        "PUT", "/api/chargers/{cid}/impairments", _obj(_model_props(ImpairmentConfig)), destructive=True)
    add("drop_connection", "Drop the charger's connection now (simulated network failure); it reconnects after "
        "reconnect_after_s (default: impairments.down_s). Transactions are kept.",
        "POST", "/api/chargers/{cid}/drop",
        _obj({"mode": {"type": "string", "enum": ["abort", "close"],
                       "description": "abort = no close frame (pulled cable), close = close code 1001"},
              "reconnect_after_s": {"type": "number", "minimum": 0, "maximum": 86400}}), destructive=True)
    add("send_frame", "Expert use: send an arbitrary charger -> CSMS CALL (action + payload, checked against the "
        "OCPP schema unless validate is false) and return the CSMS answer, or write a raw frame verbatim. The "
        "simulated station's state does not change.", "POST", "/api/chargers/{cid}/send",
        _obj({"action": {"type": "string", "minLength": 1, "description": "OCPP action, e.g. Heartbeat"},
              "payload": {"description": "CALL payload (JSON object)", "default": {}},
              "validate": {"type": "boolean", "default": True},
              "raw": {"type": "string", "minLength": 1, "description": "A raw frame text instead of action"},
              "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 60}}), destructive=True)

    # ---- scenarios (v0.3 M3)
    add("run_scenario", "Load a scenario (YAML text, format ocppsim-scenario/1: tracks of timed actions and "
        "assertions on frames / state per charger id) and start it. One scenario at a time; poll "
        "get_scenario_status, then get_scenario_result for the verdict.", "POST", "/api/scenarios/run",
        _obj({"body": {"type": "string", "minLength": 1,
                       "description": "The scenario YAML (or JSON) text, or builtin:<name> for a built-in scenario "
                                      "(list_builtin_scenarios)"}},
             ["body"], cid=False), body="text", destructive=True)
    add("list_builtin_scenarios", "The scenarios shipped with OCPPSim (name, title, description); run one with "
        "run_scenario body 'builtin:<name>'.", "GET", "/api/scenarios/builtin", no_args)
    add("get_scenario_status", "State of the current / last scenario: running / done, per-track progress, step "
        "counts, operator prompts of await_csms steps.", "GET", "/api/scenarios/current", no_args)
    add("get_scenario_result", "Verdict of the last finished scenario (ocppsim-verdict/1): result pass / fail / "
        "error / aborted and every step with status, message and evidence frame seqs.",
        "GET", "/api/scenarios/result", no_args)
    add("abort_scenario", "Stop the running scenario (result aborted).", "POST", "/api/scenarios/abort", no_args,
        destructive=True)
    add("expect_frame", "One-shot assertion: wait up to 'within' seconds for a frame in the log after seq "
        "'since' (default: now) matching charger / dir / action / message_type and payload matchers (dotted path "
        "-> literal or {in, gt, ge, lt, le, re, exists}). Returns matched and the frame. Not to miss a frame caused "
        "by your own action, read get_frames' last_seq before the action and pass it as since; a warning says "
        "when since is older than the log (oldest_seq).", "POST",
        "/api/frames/expect", _obj(_model_props(ExpectFrameBody), cid=False), destructive=False)

    # ---- traffic generator (v0.3 M4)
    add("get_traffic", "Standing traffic generator: state, the traffic: settings, counters (arrivals, skipped, "
        "started, rejected, completed, aborted, failed, preempted, cancelled), active sessions, total power of its chargers and recent "
        "events.", "GET", "/api/traffic", no_args)
    add("set_traffic", "Replace the traffic: block (seed, chargers, interval_s, probability, hourly_weights, "
        "auth_order, stop_after, duration_s, stop_by, max_session_s, idle_after_s, id_tags, id_tag_strategy "
        "random / round_robin / affinity, affinity_prob, affinity_map, vehicles). Omitted fields get their "
        "defaults, including enabled: false (send enabled: true to keep it running). A change restarts running "
        "traffic; toggling enabled starts / stops it.", "PUT", "/api/traffic",
        _obj(_model_props(TrafficConfig), cid=False), destructive=True)
    add("start_traffic", "Start the standing traffic now (not saved; traffic.enabled unchanged).", "POST",
        "/api/traffic/start", no_args, destructive=True)
    add("stop_traffic", "Stop the standing traffic; its sessions are stopped and unplugged.", "POST",
        "/api/traffic/stop", no_args, destructive=True)
    add("preview_traffic_plan", "The arrivals (sim time, charger, connector, id tag, vehicle, SoC, duration, idle) "
        "the traffic: block plans for the next hours of sim time; deterministic for a seed and start (default: "
        "the running traffic's start, else now). Arrivals on a busy connector are skipped at run time.", "GET",
        "/api/traffic/plan",
        _obj({"hours": {"type": "number", "exclusiveMinimum": 0, "maximum": 168, "default": 1},
              "charger": {"type": "string", "minLength": 1, "description": "Only this charger"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000},
              "start": {"type": "string", "description": "ISO 8601 start time of the plan"}}, cid=False))

    # ---- built-in Mini CSMS
    add("mini_csms_status", "Built-in Mini CSMS: status and the stations connected to it (error when "
        "mini_csms.enabled is off).", "GET", "/api/csms", no_args)
    add("mini_csms_actions", "Built-in Mini CSMS: the CSMS -> charger actions mini_csms_call can send for an OCPP "
        "version.", "GET", "/api/csms/actions",
        _obj({"version": {"type": "string", "enum": ["1.6", "2.0.1", "2.1"]}}, ["version"], cid=False))
    add("mini_csms_call", "Built-in Mini CSMS: send a CSMS -> charger CALL (e.g. RemoteStartTransaction, "
        "SetChargingProfile) to a connected station and return its answer.", "POST", "/api/csms/chargers/{cid}/call",
        _obj({"action": {"type": "string", "minLength": 1}, "payload": {"type": "object", "default": {}},
              "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 300}}, ["action"]), destructive=True)
    return mcp


def mount(app: Any, host: Optional[str] = None) -> McpRegistry:
    """Register the tools and serve them at ``POST /mcp`` on ``app`` (call before the app starts)."""
    mcp = build_registry(app)
    mcp.mount(app, host=host)
    return mcp
