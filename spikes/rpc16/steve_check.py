"""Interop check of the spike charge point against a real CSMS: SteVe (OCPP 1.6J).

Usage (SteVe started via its docker compose, see README.md):
    python steve_check.py --compose-dir <steve checkout> [--host localhost]

Steps:
  a. subprotocol negotiation (ocpp1.6 / ocpp2.0.1 only / none)
  b. BootNotification (registered + unregistered id), Heartbeat, StatusNotification 0/1
  c. Authorize (unknown tag), StartTransaction -> MeterValues -> StopTransaction, DB check
  d. CSMS-initiated GetConfiguration via the SteVe web UI form (login + CSRF)
  e. all frames received from SteVe are dumped to --frames for manual spec review

The SteVe defaults (admin/1234 web user, steve/changeme DB user) are its public
test defaults from application-docker.properties / docker-compose.yml.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import http.cookiejar
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from cp import ChargePoint16
from rpc import OCPP16_ERRORS, CallError, RpcConnection

CP_ID = "SME-SIM-1"
UNREG_ID = "SME-SIM-UNREGISTERED"
KNOWN_TAG = "SMETAG1"
UNKNOWN_TAG = "NOSUCHTAG9"

frames: list[dict] = []
results: dict[str, object] = {}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def on_frame(cp_id: str):
    def cb(direction: str, raw: str) -> None:
        frames.append({"cp": cp_id, "dir": direction, "t": now(), "raw": raw})
    return cb


# ------------------------------------------------------------------ DB helpers
def sql(compose_dir: str, query: str) -> str:
    cmd = ["docker", "compose", "exec", "-T", "db", "mariadb", "-usteve", "-pchangeme",
           "stevedb", "-B", "-e", query]
    out = subprocess.run(cmd, cwd=compose_dir, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else "SQL ERROR: " + out.stderr.strip()


# ------------------------------------------------------------------ a. subprotocol
async def probe_subprotocol(base: str, cp_id: str, offered: list[str] | None) -> dict:
    try:
        async with connect(f"{base}/{cp_id}", subprotocols=offered, open_timeout=10) as ws:
            return {"offered": offered, "accepted": True, "selected": ws.subprotocol}
    except InvalidStatus as e:
        return {"offered": offered, "accepted": False, "http_status": e.response.status_code}
    except Exception as e:  # noqa: BLE001
        return {"offered": offered, "accepted": False, "error": repr(e)}


# ------------------------------------------------------------------ web UI client
class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden: dict[str, str] = {}
        self.options: list[tuple[str, str]] = []  # (select name, option value)
        self._select: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("type") == "hidden" and a.get("name"):
            self.hidden.setdefault(a["name"], a.get("value") or "")
        elif tag == "input" and a.get("type") == "checkbox" and a.get("name"):
            self.options.append((a["name"], a.get("value") or ""))
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select:
            self.options.append((self._select, a.get("value") or ""))

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None


class SteveWeb:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path: str) -> tuple[str, str]:
        with self.opener.open(self.base + path, timeout=15) as r:
            return r.geturl(), r.read().decode("utf-8", "replace")

    def post(self, path: str, fields: list[tuple[str, str]]) -> tuple[str, str]:
        data = urllib.parse.urlencode(fields).encode()
        with self.opener.open(self.base + path, data=data, timeout=30) as r:
            return r.geturl(), r.read().decode("utf-8", "replace")

    @staticmethod
    def parse(html: str) -> _Forms:
        p = _Forms()
        p.feed(html)
        return p

    def login(self, user: str, password: str) -> bool:
        _, html = self.get("/manager/signin")
        csrf = self.parse(html).hidden.get("_csrf", "")
        url, _ = self.post("/manager/signin",
                           [("username", user), ("password", password), ("_csrf", csrf)])
        return "signin" not in url

    def get_configuration(self, cp_id: str) -> str:
        path = "/manager/operations/v1.6/GetConfiguration"
        _, html = self.get(path)
        form = self.parse(html)
        values = [v for n, v in form.options if n == "chargePointSelectList" and cp_id in v]
        if not values:
            raise RuntimeError(f"{cp_id} not selectable on GetConfiguration form")
        fields = [(k, v) for k, v in form.hidden.items()]
        fields += [("chargePointSelectList", values[0]), ("confKeyList", "HeartbeatInterval")]
        url, _ = self.post(path, fields)
        return url  # redirected to the task page


# ------------------------------------------------------------------ b-d. CP session
async def session(base: str, args) -> None:
    cp = ChargePoint16(base, CP_ID, on_frame=on_frame(CP_ID))  # reuse its handlers/config
    async with connect(cp.url, subprotocols=["ocpp1.6"], open_timeout=10) as ws:
        rpc = RpcConnection(ws, {"RemoteStartTransaction": cp._on_remote_start,
                                 "GetConfiguration": cp._on_get_configuration},
                            error_codes=OCPP16_ERRORS, call_timeout=15,
                            on_frame=on_frame(CP_ID), name=CP_ID)
        rx = asyncio.create_task(rpc.run())

        async def call(action, payload):
            try:
                res = await rpc.call(action, payload)
            except CallError as e:
                res = {"CALLERROR": e.code, "desc": e.description}
            results[action if action not in results else f"{action}#{len(results)}"] = res
            return res

        try:
            await call("BootNotification", {"chargePointVendor": "SME",
                                            "chargePointModel": "OCPPSim-spike"})
            await call("Heartbeat", {})
            for c in (0, 1):
                await call("StatusNotification", {"connectorId": c, "errorCode": "NoError",
                                                  "status": "Available", "timestamp": now()})
            await call("Authorize", {"idTag": UNKNOWN_TAG})
            await call("Authorize", {"idTag": KNOWN_TAG})
            await call("StatusNotification", {"connectorId": 1, "errorCode": "NoError",
                                              "status": "Preparing", "timestamp": now()})
            st = await call("StartTransaction", {"connectorId": 1, "idTag": KNOWN_TAG,
                                                 "meterStart": 1000, "timestamp": now()})
            tx = st.get("transactionId")
            await call("StatusNotification", {"connectorId": 1, "errorCode": "NoError",
                                              "status": "Charging", "timestamp": now()})
            for i, wh in enumerate((1500, 2000)):
                await call("MeterValues", {"connectorId": 1, "transactionId": tx, "meterValue": [{
                    "timestamp": now(),
                    "sampledValue": [
                        {"value": str(wh), "context": "Sample.Periodic", "measurand":
                         "Energy.Active.Import.Register", "unit": "Wh", "location": "Outlet"},
                        {"value": "7400", "context": "Sample.Periodic", "measurand":
                         "Power.Active.Import", "unit": "W", "location": "Outlet"},
                    ]}]})
            # d. CSMS-initiated operation while the connection is up
            web = SteveWeb(args.http)
            ok = await asyncio.to_thread(web.login, "admin", "1234")
            results["web_login"] = ok
            if ok:
                task_url = await asyncio.to_thread(web.get_configuration, CP_ID)
                results["GetConfiguration_task_url"] = task_url
                for _ in range(20):
                    if cp.stats.remote_calls:
                        break
                    await asyncio.sleep(0.5)
                results["remote_calls_received"] = cp.stats.remote_calls
                await asyncio.sleep(1.0)
                _, html = await asyncio.to_thread(web.get, task_url.split(args.http, 1)[1])
                text = re.sub(r"<[^>]+>", " ", html)
                results["task_page_excerpt"] = " ".join(text.split())[-600:]
            await call("StopTransaction", {"transactionId": tx, "idTag": KNOWN_TAG,
                                           "meterStop": 2500, "timestamp": now(),
                                           "reason": "Local"})
            await call("StatusNotification", {"connectorId": 1, "errorCode": "NoError",
                                              "status": "Available", "timestamp": now()})
        finally:
            await ws.close()
            await rx


async def unregistered_boot(base: str) -> dict:
    out: dict = {}
    try:
        async with connect(f"{base}/{UNREG_ID}", subprotocols=["ocpp1.6"], open_timeout=10) as ws:
            out["ws"] = "accepted"
            rpc = RpcConnection(ws, {}, call_timeout=10, on_frame=on_frame(UNREG_ID), name=UNREG_ID)
            rx = asyncio.create_task(rpc.run())
            try:
                out["boot"] = await rpc.call("BootNotification", {
                    "chargePointVendor": "SME", "chargePointModel": "OCPPSim-spike"})
                try:
                    out["heartbeat"] = await rpc.call("Heartbeat", {})
                except CallError as e:
                    out["heartbeat"] = {"CALLERROR": e.code, "desc": e.description}
            except Exception as e:  # noqa: BLE001
                out["boot"] = repr(e)
            finally:
                await ws.close()
                await rx
    except InvalidStatus as e:
        out["ws"] = f"rejected HTTP {e.response.status_code}"
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compose-dir", required=True)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--frames", default="steve_frames.jsonl")
    args = ap.parse_args()
    base = f"ws://{args.host}:8180/steve/websocket/CentralSystemService"
    args.http = f"http://{args.host}:8180/steve"

    sql(args.compose_dir,
        f"INSERT IGNORE INTO charge_box (charge_box_id) VALUES ('{CP_ID}');"
        f"INSERT IGNORE INTO ocpp_tag (id_tag) VALUES ('{KNOWN_TAG}');")

    results["subprotocol"] = [await probe_subprotocol(base, CP_ID, o) for o in
                              (["ocpp1.6"], ["ocpp2.0.1"], ["ocpp1.6", "ocpp2.0.1"], None)]
    await session(base, args)
    results["unregistered"] = await unregistered_boot(base)
    results["db_transactions"] = sql(
        args.compose_dir,
        "SELECT * FROM `transaction` ORDER BY transaction_pk DESC LIMIT 2;")
    results["db_meter_values"] = sql(
        args.compose_dir,
        "SELECT * FROM connector_meter_value ORDER BY value_timestamp DESC LIMIT 4;")

    with open(args.frames, "w", encoding="utf-8") as f:
        for fr in frames:
            f.write(json.dumps(fr) + "\n")
    print(json.dumps(results, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main())
