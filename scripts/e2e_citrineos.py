"""Manual interop check of ChargerRuntime (OCPP 2.0.1) against CitrineOS; see scripts/citrineos.md.

Usage (CitrineOS started with ``docker compose up -d`` in its citrineos-core checkout):
    .venv/Scripts/python scripts/e2e_citrineos.py --compose-dir <citrineos-core checkout> [--host localhost]

Steps:
  a. register a known id token in the CitrineOS DB (docker compose exec ocpp-db psql); the station itself is
     not registered: the Security Profile 0 server (port 8081) allows unknown charging stations
  b. ChargerRuntime connects with ``ocpp2.0.1``: BootNotification Accepted, StatusNotification, Heartbeat;
     every CSMS call that CitrineOS sends after boot (GetBaseReport, SetVariables, ...) is answered
  c. an unknown token is refused; plug + known token -> Authorize -> TransactionEvent Started ->
     Updated (meter values) -> stop -> Ended
  d. CSMS-initiated GetVariables and RequestStartTransaction / RequestStopTransaction through the REST
     message API (port 8080)
  e. the transaction rows in the CitrineOS DB; every frame is written to --frames (JSONL)

With ``--profile3 <cert-dir>`` (output of scripts/mtls_certs.py, CitrineOS started with
scripts/citrineos-mtls.compose.yml) the same flow runs as station SME-SIM-203 over the Security Profile 3 server
(``wss://<host>:8444``, TLS client certificate, no Basic Auth), followed by two connects that must be refused:
no client certificate and a client certificate from an untrusted CA.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sme_ocpp_sim.config import ChargerConfig, ReconnectConfig  # noqa: E402
from sme_ocpp_sim.framelog import FrameLog  # noqa: E402
from sme_ocpp_sim.runtime.charger import ChargerRuntime  # noqa: E402

import mtls_certs  # noqa: E402  (scripts/)

CP_ID = "SME-SIM-201"
SP3_ID = "SME-SIM-203"
# ISO14443 tokens: CitrineOS enforces the 2.0.1 IdTokenEnumType note (a 4 or 7 byte UID = 8 or 14 hex digits).
KNOWN_TOKEN = "5AE1C201"
UNKNOWN_TOKEN = "0BADC0DE"
TENANT = 1
# Transactions."stationId" is the integer key of ChargingStations, not the OCPP identity.
def _station_pk() -> str:
    return f"SELECT id FROM \"ChargingStations\" WHERE \"ocppConnectionName\" = '{CP_ID}'"

results: dict = {}


def sql(compose_dir: str, query: str) -> str:
    cmd = ["docker", "compose", "exec", "-T", "ocpp-db", "psql", "-U", "citrine", "-d", "citrine", "-A", "-F", "|",
           "-c", query]
    out = subprocess.run(cmd, cwd=compose_dir, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"SQL failed ({out.returncode}): {out.stderr.strip()}")
    return out.stdout.strip()


_bearer: dict = {}


def rest(http: str, path: str, body: dict) -> object:
    """POST to the CitrineOS REST API. The API wants a bearer token; the stack's development endpoint
    ``POST /dev/token`` mints one with the default local-dev roles."""
    if not _bearer and not path.startswith("/dev/"):
        _bearer["Authorization"] = "Bearer " + rest(http, "/dev/token", {})["accessToken"]
    req = urllib.request.Request(http + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **_bearer})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode("utf-8", "replace")[:500]}


async def _until(cond, timeout: float = 30.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.2)
        return True
    except TimeoutError:
        return False


def _msg(e) -> list:
    try:
        return json.loads(e.raw)
    except ValueError:
        return []


def _payload(e) -> dict:
    """CALL -> payload, CALLRESULT -> payload; anything else -> {}."""
    m = _msg(e)
    p = m[3] if e.message_type == "CALL" and len(m) > 3 else m[2] if e.message_type == "CALLRESULT" and len(m) > 2 else {}
    return p if isinstance(p, dict) else {}


def _calls(log: FrameLog, action: str, direction: str) -> list:
    return [e for e in log.since(0, charger=CP_ID, action=action) if e.message_type == "CALL" and e.direction == direction]


def _tx_events(log: FrameLog, event_type: str) -> list:
    return [e for e in _calls(log, "TransactionEvent", "tx") if _payload(e).get("eventType") == event_type]


def _message_api() -> str:
    return f"?identifier={CP_ID}&tenantId={TENANT}"


async def flow(base: str, http: str, log: FrameLog, tls: dict) -> None:
    cfg = ChargerConfig(id=CP_ID, vendor="SME", model="OCPPSim", auth_mode="csms_only", max_power_w=7400,
                        meter_sample_interval_s=5, **tls)
    rt = ChargerRuntime(cfg, base, "2.0.1", frame_log=log, call_timeout=20)
    stop = asyncio.Event()
    task = asyncio.create_task(rt.run(stop))
    try:
        results["online"] = await _until(lambda: rt.online)
        boot_uids = {e.uid for e in _calls(log, "BootNotification", "tx")}
        boot = [e for e in log.since(0, charger=CP_ID) if e.message_type == "CALLRESULT" and e.uid in boot_uids]
        results["boot_status"] = _payload(boot[-1]).get("status") if boot else None
        results["status_notification"] = len(_calls(log, "StatusNotification", "tx"))
        # Let CitrineOS run its post-boot sequence (GetBaseReport, SetVariables, ...).
        await asyncio.sleep(15)
        results["csms_calls_after_boot"] = _answered(log)

        results["swipe_unknown"] = str(await rt.swipe(1, UNKNOWN_TOKEN))
        await rt.plug(1)
        results["swipe_known"] = str(await rt.swipe(1, KNOWN_TOKEN))
        results["tx_started"] = await _until(lambda: bool(_tx_events(log, "Started")), 15)
        results["tx_updated_meter_values"] = await _until(
            lambda: sum(1 for e in _tx_events(log, "Updated") if _payload(e).get("meterValue")) >= 2, 20)

        n = len(_calls(log, "GetVariables", "rx"))
        results["rest_GetVariables"] = await asyncio.to_thread(
            rest, http, "/ocpp/2.0.1/monitoring/getVariables" + _message_api(),
            {"getVariableData": [{"component": {"name": "OCPPCommCtrlr"}, "variable": {"name": "HeartbeatInterval"}}]})
        results["GetVariables_served"] = await _until(lambda: len(_calls(log, "GetVariables", "rx")) > n, 10)

        results["stop_tx"] = await rt.stop_tx(1)
        results["tx_ended"] = await _until(lambda: bool(_tx_events(log, "Ended")), 10)

        # CSMS-initiated start/stop while still plugged in.
        n = len(_tx_events(log, "Started"))
        results["rest_RequestStartTransaction"] = await asyncio.to_thread(
            rest, http, "/ocpp/2.0.1/evdriver/requestStartTransaction" + _message_api(),
            {"remoteStartId": 4711, "evseId": 1, "idToken": {"idToken": KNOWN_TOKEN, "type": "ISO14443"}})
        results["remote_start_tx_started"] = await _until(lambda: len(_tx_events(log, "Started")) > n, 15)
        tx_id = None
        started = _tx_events(log, "Started")
        if started:
            tx_id = _payload(started[-1]).get("transactionInfo", {}).get("transactionId")
        if results["remote_start_tx_started"] and tx_id:
            n = len(_tx_events(log, "Ended"))
            results["rest_RequestStopTransaction"] = await asyncio.to_thread(
                rest, http, "/ocpp/2.0.1/evdriver/requestStopTransaction" + _message_api(),
                {"transactionId": tx_id})
            results["remote_stop_tx_ended"] = await _until(lambda: len(_tx_events(log, "Ended")) > n, 15)
        await rt.unplug(1)
        # CitrineOS' boot interval is 60 s; the run has usually passed it by now.
        results["heartbeat"] = await _until(lambda: rt.last_heartbeat is not None, 70) or "no heartbeat within 70 s"
        results["unanswered_or_errors"] = _errors(log)
        results["snapshot"] = rt.snapshot()
    finally:
        stop.set()
        async with asyncio.timeout(10):
            await task


def _answered(log: FrameLog) -> dict:
    """CSMS -> CS calls with the answer the station gave (CALLRESULT status or CALLERROR code)."""
    out: dict = {}
    entries = log.since(0, charger=CP_ID)
    replies = {e.uid: e for e in entries if e.direction == "tx" and e.message_type in ("CALLRESULT", "CALLERROR")}
    for e in entries:
        if e.direction == "rx" and e.message_type == "CALL":
            r = replies.get(e.uid)
            if r is None:
                answer = "unanswered"
            elif r.message_type == "CALLERROR":
                answer = f"CALLERROR {_msg(r)[2:3]}"
            else:
                answer = _payload(r).get("status", "ok")
            out.setdefault(e.action, []).append(answer)
    return out


def _errors(log: FrameLog) -> list:
    return [{"dir": e.direction, "uid": e.uid, "error": _msg(e)[2:4]}
            for e in log.since(0, charger=CP_ID) if e.message_type == "CALLERROR"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compose-dir", required=True)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--frames", default="citrineos_frames.jsonl")
    ap.add_argument("--profile3", metavar="CERT_DIR", help="Security Profile 3 run (scripts/mtls_certs.py output)")
    args = ap.parse_args()
    global CP_ID
    base = f"ws://{args.host}:8081"
    tls: dict = {}
    if args.profile3:
        CP_ID, base = SP3_ID, f"wss://{args.host}:8444"
        tls = mtls_certs.tls_fields(Path(args.profile3), SP3_ID, "good")
    http = f"http://{args.host}:8080"

    sql(args.compose_dir,
        'INSERT INTO "Authorizations" ("tenantId", "idToken", "idTokenType", "status", "isPrepaid", "createdAt", '
        '"updatedAt") '
        f"SELECT {TENANT}, '{KNOWN_TOKEN}', 'ISO14443', 'Accepted', false, now(), now() "
        f"WHERE NOT EXISTS (SELECT 1 FROM \"Authorizations\" WHERE \"idToken\" = '{KNOWN_TOKEN}');")
    log = FrameLog()
    try:
        await flow(base, http, log, tls)
        if args.profile3:
            results["sp3_rejections"] = await mtls_certs.sp3_rejections(lambda kind: ChargerRuntime(
                ChargerConfig(id=SP3_ID, **mtls_certs.tls_fields(Path(args.profile3), SP3_ID, kind)), base, "2.0.1",
                frame_log=log, reconnect=ReconnectConfig(min_s=1, max_s=2)))
    finally:
        Path(args.frames).write_text(log.to_jsonl(), encoding="utf-8")
        print(json.dumps(results, indent=1, default=str), flush=True)
    db = {
        "db_transactions": sql(
            args.compose_dir,
            'SELECT "transactionId", "evseId", "isActive", "chargingState", "totalKwh", "stoppedReason", '
            f"\"remoteStartId\" FROM \"Transactions\" WHERE \"stationId\" = ({_station_pk()}) ORDER BY id DESC LIMIT 3;"),
        "db_transaction_events": sql(
            args.compose_dir,
            'SELECT t."transactionId", e."eventType", e."triggerReason", e."seqNo", e."idTokenValue" '
            'FROM "TransactionEvents" e JOIN "Transactions" t ON t.id = e."transactionDatabaseId" '
            f"WHERE t.\"stationId\" = ({_station_pk()}) ORDER BY e.id DESC LIMIT 10;"),
        "db_meter_values": sql(
            args.compose_dir,
            'SELECT count(*) FROM "MeterValues" m JOIN "Transactions" t ON t.id = m."transactionDatabaseId" '
            f"WHERE t.\"stationId\" = ({_station_pk()});"),
    }
    print(json.dumps(db, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main())
