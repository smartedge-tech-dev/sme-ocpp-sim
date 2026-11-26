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

    @on("BootNotification")
    async def on_boot(self, charge_point_vendor, charge_point_model, **_):
        self.session.boots += 1
        self.session.booted.set()
        return call_result.BootNotification(
            current_time=_now(), interval=self.hb_interval, status="Accepted")

    @on("Heartbeat")
    async def on_heartbeat(self):
        self.session.heartbeats += 1
        return call_result.Heartbeat(current_time=_now())


class CsmsPeer:
    def __init__(self, *, hb_interval: int = 1, exercise: bool = False,
                 registered: Optional[set] = None, passwords: Optional[dict] = None,
                 ssl_context: Optional[ssl.SSLContext] = None) -> None:
        self.hb_interval = hb_interval
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
        cp = PeerCP(cp_id, TapConnection(ws, s.frames), response_timeout=5)
        cp.session, cp.hb_interval = s, self.hb_interval
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
