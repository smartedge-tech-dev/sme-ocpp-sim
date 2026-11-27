"""Minimal OCPP 1.6 CSMS used ONLY as a dev/test peer.

Built on MobilityHouse ``ocpp`` (dev dependency, never a product dependency); its schema
validation checks the payloads our charger sends. Raw frames are recorded per session.

Handshake rejections imitate a real CSMS (SteVe): unknown charger id -> HTTP 404,
wrong / missing Basic Auth -> HTTP 401.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Optional
from urllib.parse import unquote

from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppCP16
from ocpp.v16 import call, call_result
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

logging.getLogger("ocpp").setLevel(logging.WARNING)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cp_id(path: str) -> str:
    return unquote(path.rstrip("/").rsplit("/", 1)[-1])


@dataclass
class FooBarUnknownAction:
    """Payload class whose name becomes an action the charger does not implement."""


@dataclass
class Session:
    cp_id: str
    subprotocol: Optional[str]
    authorization: Optional[str]
    frames: list = field(default_factory=list)  # (dir, raw), dir seen from the CSMS
    boots: int = 0
    heartbeats: int = 0
    results: dict = field(default_factory=dict)
    booted: asyncio.Event = field(default_factory=asyncio.Event)
    exercised: asyncio.Event = field(default_factory=asyncio.Event)
    boot_statuses: list = field(default_factory=list)  # status answered per BootNotification
    statuses: list = field(default_factory=list)       # (connector_id, status, error_code)
    authorizes: list = field(default_factory=list)     # id tags
    starts: list = field(default_factory=list)         # dict per StartTransaction
    stops: list = field(default_factory=list)          # dict per StopTransaction
    meter_values: list = field(default_factory=list)   # dict per MeterValues
    data_transfers: list = field(default_factory=list)
    cp: object = None


class TapConnection:
    """Wraps the websocket so every raw frame is recorded before ``ocpp`` sees it."""

    def __init__(self, ws, frames: list) -> None:
        self._ws, self._frames = ws, frames

    async def recv(self):
        raw = await self._ws.recv()
        self._frames.append(("rx", raw))
        return raw

    async def send(self, raw):
        self._frames.append(("tx", raw))
        await self._ws.send(raw)


class PeerCP(OcppCP16):
    session: Session
    hb_interval: int
    peer: "CsmsPeer"

    @on("BootNotification")
    async def on_boot(self, charge_point_vendor, charge_point_model, **_):
        self.session.boots += 1
        status = self.peer.boot_statuses.pop(0) if self.peer.boot_statuses else "Accepted"
        self.session.boot_statuses.append(status)
        if status == "Accepted":
            self.session.booted.set()
        interval = self.hb_interval if status == "Accepted" else self.peer.retry_interval
        return call_result.BootNotification(current_time=_now(), interval=interval, status=status)

    @on("Heartbeat")
    async def on_heartbeat(self):
        self.session.heartbeats += 1
        return call_result.Heartbeat(current_time=_now())

    @on("StatusNotification")
    async def on_status(self, connector_id, error_code, status, **_):
        await self.peer.hold("StatusNotification")
        self.session.statuses.append((connector_id, status, error_code))
        return call_result.StatusNotification()

    @on("Authorize")
    async def on_authorize(self, id_tag):
        self.session.authorizes.append(id_tag)
        info = {"status": self.peer.tag_status(id_tag)}
        if id_tag in self.peer.tag_parents:
            info["parent_id_tag"] = self.peer.tag_parents[id_tag]
        return call_result.Authorize(id_tag_info=info)

    @on("StartTransaction")
    async def on_start(self, connector_id, id_tag, meter_start, timestamp, **kw):
        await self.peer.hold("StartTransaction")
        self.peer.next_tx += 1
        tx = self.peer.next_tx
        self.session.starts.append({"connector_id": connector_id, "id_tag": id_tag, "meter_start": meter_start,
                                    "transaction_id": tx, **kw})
        return call_result.StartTransaction(transaction_id=tx,
                                            id_tag_info={"status": self.peer.tag_status(id_tag)})

    @on("StopTransaction")
    async def on_stop(self, transaction_id, meter_stop, timestamp, **kw):
        await self.peer.hold("StopTransaction")
        self.session.stops.append({"transaction_id": transaction_id, "meter_stop": meter_stop,
                                   "reason": kw.get("reason"), "transaction_data": kw.get("transaction_data"),
                                   "id_tag": kw.get("id_tag")})
        return call_result.StopTransaction(id_tag_info={"status": "Accepted"})

    @on("MeterValues")
    async def on_meter_values(self, connector_id, meter_value, **kw):
        self.session.meter_values.append({"connector_id": connector_id, "meter_value": meter_value,
                                          "transaction_id": kw.get("transaction_id")})
        return call_result.MeterValues()

    @on("DataTransfer")
    async def on_data_transfer(self, vendor_id, **kw):
        self.session.data_transfers.append({"vendor_id": vendor_id, **kw})
        return call_result.DataTransfer(status="Accepted", data="pong")


class CsmsPeer:
    def __init__(self, *, hb_interval: int = 1, exercise: bool = False,
                 registered: Optional[set] = None, passwords: Optional[dict] = None,
                 ssl_context: Optional[ssl.SSLContext] = None,
                 boot_statuses: Optional[list] = None, retry_interval: int = 1,
                 tag_statuses: Optional[dict] = None, tag_parents: Optional[dict] = None) -> None:
        self.hb_interval = hb_interval
        self.boot_statuses = list(boot_statuses or [])  # consumed one per BootNotification, then Accepted
        self.retry_interval = retry_interval             # interval answered with Pending / Rejected
        self.tag_statuses = dict(tag_statuses or {})     # id tag -> status (default Accepted)
        self.tag_parents = dict(tag_parents or {})       # id tag -> parentIdTag of the Authorize answer
        self.next_tx = 0
        self.gates: dict[str, asyncio.Event] = {}        # action -> answer only once the event is set
        self.delays: dict[str, float] = {}               # action -> answer delay in seconds
        self.history: list = []                          # every Session, oldest first
        self.exercise = exercise
        self.registered = registered      # None = accept any charger id
        self.passwords = passwords        # None = no auth; {cp_id: password}
        self.ssl_context = ssl_context
        self.sessions: dict[str, Session] = {}
        self.port: Optional[int] = None
        self._server = None

    @property
    def url(self) -> str:
        scheme = "wss" if self.ssl_context else "ws"
        return f"{scheme}://127.0.0.1:{self.port}/ocpp"

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await serve(self._handler, host, port, subprotocols=["ocpp1.6"],
                                   process_request=self._check, ssl=self.ssl_context,
                                   max_queue=64)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def hold(self, action: str) -> None:
        """Delay the answer to ``action`` (``gates`` first, then ``delays``); the peer reads nothing meanwhile."""
        gate = self.gates.get(action)
        if gate is not None:
            await gate.wait()
        if self.delays.get(action):
            await asyncio.sleep(self.delays[action])

    def tag_status(self, id_tag: str) -> str:
        return self.tag_statuses.get(id_tag, "Accepted")

    async def request(self, cp_id: str, payload, **kw):
        """Send a CSMS -> CP CALL (an ``ocpp.v16.call`` object) on the current session of ``cp_id``."""
        return await self.sessions[cp_id].cp.call(payload, **kw)

    async def remote_start(self, cp_id, id_tag, connector_id=None, charging_profile=None):
        return await self.request(cp_id, call.RemoteStartTransaction(id_tag=id_tag, connector_id=connector_id,
                                                                     charging_profile=charging_profile))

    async def set_charging_profile(self, cp_id, connector_id, profile):
        """``profile``: a 1.6 ChargingProfile dict with camelCase keys."""
        return await self.request(cp_id, call.SetChargingProfile(connector_id=connector_id,
                                                                 cs_charging_profiles=profile))

    async def clear_charging_profile(self, cp_id, **kw):
        """``kw``: id / connector_id / charging_profile_purpose / stack_level."""
        return await self.request(cp_id, call.ClearChargingProfile(**kw))

    async def get_composite_schedule(self, cp_id, connector_id, duration, unit=None):
        return await self.request(cp_id, call.GetCompositeSchedule(connector_id=connector_id, duration=duration,
                                                                   charging_rate_unit=unit))

    async def remote_stop(self, cp_id, transaction_id):
        return await self.request(cp_id, call.RemoteStopTransaction(transaction_id=transaction_id))

    async def get_configuration(self, cp_id, keys=None):
        return await self.request(cp_id, call.GetConfiguration(key=keys))

    async def change_configuration(self, cp_id, key, value):
        return await self.request(cp_id, call.ChangeConfiguration(key=key, value=value))

    async def reset(self, cp_id, kind="Soft"):
        return await self.request(cp_id, call.Reset(type=kind))

    async def trigger(self, cp_id, message, connector_id=None):
        return await self.request(cp_id, call.TriggerMessage(requested_message=message,
                                                             connector_id=connector_id))

    async def unlock(self, cp_id, connector_id):
        return await self.request(cp_id, call.UnlockConnector(connector_id=connector_id))

    async def change_availability(self, cp_id, connector_id, kind):
        return await self.request(cp_id, call.ChangeAvailability(connector_id=connector_id, type=kind))

    async def data_transfer(self, cp_id, vendor_id, message_id=None, data=None):
        return await self.request(cp_id, call.DataTransfer(vendor_id=vendor_id, message_id=message_id,
                                                           data=data))

    def _check(self, connection, request):
        cp_id = _cp_id(request.path)
        if self.registered is not None and cp_id not in self.registered:
            return connection.respond(HTTPStatus.NOT_FOUND, "unknown charge box\n")
        if self.passwords is not None:
            auth = request.headers.get("Authorization") or ""
            try:
                scheme, token = auth.split(" ", 1)
                user, pw = base64.b64decode(token).decode().split(":", 1)  # RFC 7617
            except ValueError:
                user = pw = scheme = None
            if scheme != "Basic" or user != cp_id or self.passwords.get(cp_id) != pw:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "bad credentials\n")
        return None

    async def _handler(self, ws) -> None:
        cp_id = _cp_id(ws.request.path)
        s = Session(cp_id, ws.subprotocol, ws.request.headers.get("Authorization"))
        self.sessions[cp_id] = s
        self.history.append(s)
        cp = PeerCP(cp_id, TapConnection(ws, s.frames), response_timeout=5)
        cp.session, cp.hb_interval, cp.peer = s, self.hb_interval, self
        s.cp = cp
        tasks = [asyncio.create_task(cp.start())]
        if self.exercise:
            tasks.append(asyncio.create_task(self._exercise(cp, ws, s)))
        try:
            await tasks[0]
        except ConnectionClosed:
            pass
        finally:
            for t in tasks:
                t.cancel()

    async def _exercise(self, cp: PeerCP, ws, s: Session) -> None:
        await s.booted.wait()
        r = await cp.call(call.RemoteStartTransaction(id_tag="TAG001", connector_id=1))
        s.results["RemoteStartTransaction"] = r.status
        r = await cp.call(call.GetConfiguration(key=["HeartbeatInterval", "NoSuchKey"]))
        s.results["GetConfiguration"] = (r.configuration_key, r.unknown_key)
        # Unknown action through the library path (schema validation must be skipped,
        # otherwise ocpp refuses to send an action it has no schema for).
        try:
            await cp.call(FooBarUnknownAction(), suppress=False, skip_schema_validation=True)
            s.results["FooBarUnknownAction"] = "no error?!"
        except Exception as e:  # noqa: BLE001 - ocpp maps the code to its own exception class
            s.results["FooBarUnknownAction"] = getattr(e, "code", repr(e))
        # Malformed frames: the library cannot emit these -> raw websocket send.
        # Our CALLERROR answers land in ocpp's response queue as unsolicited
        # responses; 2.1.0 ignores them on the next call ("unknown unique id").
        await ws.send(json.dumps([2, "bad-payload", "Heartbeat", "not-an-object"]))
        await ws.send(json.dumps([2, "bad-shape", "Heartbeat"]))
        await ws.send(json.dumps([7, "bad-type", {}]))
        s.exercised.set()


if __name__ == "__main__":
    async def _main() -> None:
        peer = CsmsPeer(exercise=True)
        await peer.start(port=9000)
        print(f"ocpp16_peer listening on {peer.url}/<chargePointId>")
        await asyncio.Future()

    asyncio.run(_main())
