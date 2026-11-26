"""Manual interop check of ChargerRuntime against SteVe (OCPP 1.6J); see scripts/steve.md.

Usage (SteVe started with its docker compose):
    .venv/Scripts/python scripts/e2e_steve.py --compose-dir <steve checkout> [--host localhost]

Steps (port of the spike's steve_check.py onto the real runtime):
  a. register the charge box + a known id tag in the SteVe DB (docker compose exec db mariadb)
  b. ChargerRuntime connects: BootNotification, StatusNotification 0/1, Heartbeat
     (SteVe sends GetConfiguration before answering Boot; the runtime serves it)
  c. plug + swipe the known tag -> Authorize -> StartTransaction -> MeterValues -> stop -> StopTransaction,
     an unknown tag is refused
  d. CSMS-initiated GetConfiguration through the SteVe web UI (login + CSRF)
  e. an unregistered charger id: the handshake is refused (HTTP 404) and recorded as last_error "not_found"
  f. DB rows of the transaction / meter values; every frame is written to --frames (JSONL)

The SteVe defaults (admin/1234 web user, steve/changeme DB user) are its public test defaults.
"""

from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sme_ocpp_sim.config import ChargerConfig, ReconnectConfig  # noqa: E402
from sme_ocpp_sim.framelog import FrameLog  # noqa: E402
from sme_ocpp_sim.runtime.charger import ChargerRuntime  # noqa: E402

CP_ID = "SME-SIM-1"
UNREG_ID = "SME-SIM-UNREGISTERED"
KNOWN_TAG = "SMETAG1"
UNKNOWN_TAG = "NOSUCHTAG9"

results: dict = {}


def sql(compose_dir: str, query: str) -> str:
    cmd = ["docker", "compose", "exec", "-T", "db", "mariadb", "-usteve", "-pchangeme", "stevedb", "-B", "-e", query]
    out = subprocess.run(cmd, cwd=compose_dir, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else "SQL ERROR: " + out.stderr.strip()


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden: dict = {}
        self.options: list = []  # (select / checkbox name, value)
        self._select = None

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
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path: str):
        with self.opener.open(self.base + path, timeout=15) as r:
            return r.geturl(), r.read().decode("utf-8", "replace")

    def post(self, path: str, fields):
        with self.opener.open(self.base + path, data=urllib.parse.urlencode(fields).encode(), timeout=30) as r:
            return r.geturl(), r.read().decode("utf-8", "replace")

    @staticmethod
    def parse(html: str) -> _Forms:
        p = _Forms()
        p.feed(html)
        return p

    def login(self, user: str, password: str) -> bool:
        _, html = self.get("/manager/signin")
        csrf = self.parse(html).hidden.get("_csrf", "")
        url, _ = self.post("/manager/signin", [("username", user), ("password", password), ("_csrf", csrf)])
        return "signin" not in url

    def get_configuration(self, cp_id: str) -> str:
        path = "/manager/operations/v1.6/GetConfiguration"
        _, html = self.get(path)
        form = self.parse(html)
        values = [v for n, v in form.options if n == "chargePointSelectList" and cp_id in v]
        if not values:
            raise RuntimeError(f"{cp_id} not selectable on GetConfiguration form")
        fields = list(form.hidden.items())
        fields += [("chargePointSelectList", values[0]), ("confKeyList", "HeartbeatInterval")]
        url, _ = self.post(path, fields)
        return url


async def _until(cond, timeout: float = 30.0) -> bool:
    try:
        async def poll():
            while not cond():
                await asyncio.sleep(0.2)
        await asyncio.wait_for(poll(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


def _count(log: FrameLog, cp: str, action: str, message_type: str = "CALL") -> int:
    return sum(1 for e in log.since(0, charger=cp, action=action) if e.message_type == message_type)


async def registered_flow(base: str, http: str, log: FrameLog) -> None:
    cfg = ChargerConfig(id=CP_ID, vendor="SME", model="OCPPSim", auth_mode="csms_only", power_w=7400,
                        config_keys={"MeterValueSampleInterval": "5"})
    rt = ChargerRuntime(cfg, base, frame_log=log, call_timeout=15)
    stop = asyncio.Event()
    task = asyncio.create_task(rt.run(stop))
    try:
        results["online"] = await _until(lambda: rt.online)
        results["get_configuration_before_boot"] = any(
            e.direction == "rx" and e.action == "GetConfiguration" for e in log.since(0, charger=CP_ID))
        results["heartbeat"] = await _until(lambda: rt.last_heartbeat is not None, 5) or "no heartbeat within 5 s"
        results["swipe_unknown"] = await rt.swipe(1, UNKNOWN_TAG)
        await rt.plug(1)
        results["swipe_known"] = await rt.swipe(1, KNOWN_TAG)
        results["meter_values"] = await _until(lambda: _count(log, CP_ID, "MeterValues") >= 2, 15)

        web = SteveWeb(http)
        ok = await asyncio.to_thread(web.login, "admin", "1234")
        results["web_login"] = ok
        if ok:
            n = sum(1 for e in log.since(0, charger=CP_ID, action="GetConfiguration") if e.direction == "rx")
            task_url = await asyncio.to_thread(web.get_configuration, CP_ID)
            results["GetConfiguration_task_url"] = task_url
            results["GetConfiguration_served"] = await _until(lambda: sum(
                1 for e in log.since(0, charger=CP_ID, action="GetConfiguration") if e.direction == "rx") > n, 10)
            await asyncio.sleep(1.0)
            _, html = await asyncio.to_thread(web.get, task_url.split(http, 1)[1])
            results["task_page_excerpt"] = " ".join(re.sub(r"<[^>]+>", " ", html).split())[-600:]

        results["stop_tx"] = await rt.stop_tx(1)
        await rt.unplug(1)
        await asyncio.sleep(1.0)
        results["snapshot"] = rt.snapshot()
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def unregistered_flow(base: str, log: FrameLog) -> None:
    rt = ChargerRuntime(ChargerConfig(id=UNREG_ID), base, frame_log=log,
                        reconnect=ReconnectConfig(min_s=1, max_s=2))
    stop = asyncio.Event()
    task = asyncio.create_task(rt.run(stop))
    try:
        await _until(lambda: rt.connect_attempts >= 2 and rt.last_error is not None, 15)
        results["unregistered"] = {"last_error": rt.last_error, "attempts": rt.connect_attempts,
                                   "connected": rt.connected}
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compose-dir", required=True)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--frames", default="steve_frames.jsonl")
    args = ap.parse_args()
    base = f"ws://{args.host}:8180/steve/websocket/CentralSystemService"
    http = f"http://{args.host}:8180/steve"

    sql(args.compose_dir, f"INSERT IGNORE INTO charge_box (charge_box_id) VALUES ('{CP_ID}');"
                          f"INSERT IGNORE INTO ocpp_tag (id_tag) VALUES ('{KNOWN_TAG}');")
    log = FrameLog()
    await registered_flow(base, http, log)
    await unregistered_flow(base, log)
    results["db_transactions"] = sql(args.compose_dir,
                                     "SELECT * FROM `transaction` ORDER BY transaction_pk DESC LIMIT 2;")
    results["db_meter_values"] = sql(args.compose_dir,
                                     "SELECT * FROM connector_meter_value ORDER BY value_timestamp DESC LIMIT 4;")
    Path(args.frames).write_text(log.to_jsonl(), encoding="utf-8")
    print(json.dumps(results, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main())
