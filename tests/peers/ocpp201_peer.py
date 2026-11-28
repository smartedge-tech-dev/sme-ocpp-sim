"""Minimal OCPP 2.0.1 CSMS used ONLY as a dev/test peer.

Built on MobilityHouse ``ocpp`` (``ocpp.v201``; dev dependency, never a product dependency); its schema
validation checks the payloads our charger sends. Raw frames are recorded per session. Same shape as
``ocpp16_peer.py`` but independent of it: the 2.0.1 stack shares nothing with 1.6.

Handshake rejections imitate a real CSMS: unknown station id -> HTTP 404, wrong / missing Basic Auth
-> HTTP 401.

Known library behaviour (design §8): a frame the library cannot parse is logged and not answered, and
a CALLERROR answer is suppressed unless ``suppress=False``; assert on the charger-side frame log.
"""

from __future__ import annotations

import asyncio
import os
import base64
import logging
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Optional
from urllib.parse import unquote

from ocpp.routing import on
from ocpp.v201 import ChargePoint as OcppCP201
from ocpp.v201 import call, call_result
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

logging.getLogger("ocpp").setLevel(logging.WARNING)

SUBPROTOCOL = "ocpp2.1" if os.environ.get("OCPPSIM_V201_SUITE") == "2.1" else "ocpp2.0.1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cs_id(path: str) -> str:
    return unquote(path.rstrip("/").rsplit("/", 1)[-1])


@dataclass
class Session:
    cs_id: str
    subprotocol: Optional[str]
    authorization: Optional[str]
    frames: list = field(default_factory=list)  # (dir, raw), dir seen from the CSMS
    boots: int = 0
    heartbeats: int = 0
    booted: asyncio.Event = field(default_factory=asyncio.Event)
    boot_statuses: list = field(default_factory=list)       # status answered per BootNotification
    boot_requests: list = field(default_factory=list)       # dict per BootNotification
    statuses: list = field(default_factory=list)            # (evse_id, connector_id, connector_status)
    authorizes: list = field(default_factory=list)          # idToken dicts
    transaction_events: list = field(default_factory=list)  # dict per TransactionEvent
    meter_values: list = field(default_factory=list)        # dict per MeterValues
    data_transfers: list = field(default_factory=list)
    reports: list = field(default_factory=list)            # dict per NotifyReport
    profile_reports: list = field(default_factory=list)    # dict per ReportChargingProfiles
    charging_limits: list = field(default_factory=list)    # dict per NotifyChargingLimit
    cleared_limits: list = field(default_factory=list)     # dict per ClearedChargingLimit
    log_statuses: list = field(default_factory=list)       # status per LogStatusNotification
    firmware_statuses: list = field(default_factory=list)  # status per FirmwareStatusNotification
    log_request_ids: list = field(default_factory=list)       # requestId per LogStatusNotification
    firmware_request_ids: list = field(default_factory=list)  # requestId per FirmwareStatusNotification
    reservation_updates: list = field(default_factory=list)  # (reservationId, status) per ReservationStatusUpdate
    security_events: list = field(default_factory=list)  # (type, techInfo) per SecurityEventNotification
    csrs: list = field(default_factory=list)             # (csr, certificateType) per SignCertificate
    peercert: object = None  # the TLS client certificate (ssl getpeercert dict) of a wss:// session
    cs: object = None


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
        try:
            await self._ws.send(raw)
        except ConnectionClosed:
            # An answer to a station CALL that can no longer be sent (the station closed the session meanwhile, e.g.
            # right after answering a password change or Reset) must not end ``ocpp``'s receive loop: frames the
            # station sent before closing - such as the answer to a CALL of ours still waiting in ``call()`` - are
            # still buffered and would otherwise be dropped (``call()`` then times out). ``recv`` raises once they
            # are read. A CALL of ours still fails at once.
            if raw.lstrip("[ ").startswith("2"):
                raise


class PeerCS(OcppCP201):
    session: Session
    peer: "CsmsPeer201"

    @on("BootNotification")
    async def on_boot(self, charging_station, reason, **kw):
        s = self.session
        s.boots += 1
        s.boot_requests.append({"charging_station": charging_station, "reason": reason, **kw})
        status = self.peer.boot_statuses.pop(0) if self.peer.boot_statuses else "Accepted"
        s.boot_statuses.append(status)
        if status == "Accepted":
            s.booted.set()
        interval = self.peer.hb_interval if status == "Accepted" else self.peer.retry_interval
        return call_result.BootNotification(current_time=_now(), interval=interval, status=status)

    @on("Heartbeat")
    async def on_heartbeat(self, **_):
        self.session.heartbeats += 1
        return call_result.Heartbeat(current_time=_now())

    @on("StatusNotification")
    async def on_status(self, timestamp, connector_status, evse_id, connector_id, **_):
        await self.peer.hold("StatusNotification")
        self.session.statuses.append((evse_id, connector_id, connector_status))
        return call_result.StatusNotification()

    @on("Authorize")
    async def on_authorize(self, id_token, **_):
        await self.peer.hold("Authorize")
        self.session.authorizes.append(id_token)
        info = {"status": self.peer.token_status(id_token["id_token"])}
        group = self.peer.token_groups.get(id_token["id_token"])
        if group is not None:
            info["group_id_token"] = {"id_token": group, "type": "Central"}
        return call_result.Authorize(id_token_info=info)

    @on("TransactionEvent")
    async def on_transaction_event(self, event_type, timestamp, trigger_reason, seq_no, transaction_info, **kw):
        await self.peer.hold("TransactionEvent")
        self.session.transaction_events.append({
            "event_type": event_type, "timestamp": timestamp, "trigger_reason": trigger_reason,
            "seq_no": seq_no, "transaction_info": transaction_info, **kw,
        })
        id_token = kw.get("id_token")
        if id_token is None:
            return call_result.TransactionEvent()
        return call_result.TransactionEvent(id_token_info={"status": self.peer.token_status(id_token["id_token"])})

    @on("MeterValues")
    async def on_meter_values(self, evse_id, meter_value, **_):
        self.session.meter_values.append({"evse_id": evse_id, "meter_value": meter_value})
        return call_result.MeterValues()

    @on("NotifyReport")
    async def on_notify_report(self, request_id, generated_at, seq_no, **kw):
        self.session.reports.append({"request_id": request_id, "generated_at": generated_at, "seq_no": seq_no,
                                     **kw})
        return call_result.NotifyReport()

    @on("ReportChargingProfiles")
    async def on_report_charging_profiles(self, request_id, charging_limit_source, charging_profile, evse_id, **kw):
        self.session.profile_reports.append({"request_id": request_id, "charging_limit_source": charging_limit_source,
                                             "charging_profile": charging_profile, "evse_id": evse_id, **kw})
        return call_result.ReportChargingProfiles()

    @on("NotifyChargingLimit")
    async def on_notify_charging_limit(self, charging_limit, **kw):
        self.session.charging_limits.append({"charging_limit": charging_limit, **kw})
        return call_result.NotifyChargingLimit()

    @on("ClearedChargingLimit")
    async def on_cleared_charging_limit(self, charging_limit_source, **kw):
        self.session.cleared_limits.append({"charging_limit_source": charging_limit_source, **kw})
        return call_result.ClearedChargingLimit()

    @on("LogStatusNotification")
    async def on_log_status(self, status, request_id=None, **_):
        self.session.log_statuses.append(status)
        self.session.log_request_ids.append(request_id)
        return call_result.LogStatusNotification()

    @on("FirmwareStatusNotification")
    async def on_firmware_status(self, status, request_id=None, **_):
        self.session.firmware_statuses.append(status)
        self.session.firmware_request_ids.append(request_id)
        return call_result.FirmwareStatusNotification()

    @on("ReservationStatusUpdate")
    async def on_reservation_status_update(self, reservation_id, reservation_update_status, **_):
        self.session.reservation_updates.append((reservation_id, reservation_update_status))
        return call_result.ReservationStatusUpdate()

    @on("DataTransfer")
    async def on_data_transfer(self, vendor_id, **kw):
        self.session.data_transfers.append({"vendor_id": vendor_id, **kw})
        return call_result.DataTransfer(status="Accepted", data="pong")

    @on("SecurityEventNotification")
    async def on_security_event(self, type, timestamp, tech_info=None, **_):
        self.session.security_events.append((type, tech_info))
        return call_result.SecurityEventNotification()

    @on("SignCertificate")
    async def on_sign_certificate(self, csr, certificate_type=None, **_):
        self.session.csrs.append((csr, certificate_type))
        return call_result.SignCertificate(status=self.peer.sign_status)


class CsmsPeer201:
    def __init__(self, *, hb_interval: int = 1, registered: Optional[set] = None,
                 passwords: Optional[dict] = None, ssl_context: Optional[ssl.SSLContext] = None,
                 boot_statuses: Optional[list] = None, retry_interval: int = 1,
                 token_statuses: Optional[dict] = None, token_groups: Optional[dict] = None) -> None:
        self.hb_interval = hb_interval
        self.boot_statuses = list(boot_statuses or [])   # consumed one per BootNotification, then Accepted
        self.retry_interval = retry_interval              # interval answered with Pending / Rejected
        self.token_statuses = dict(token_statuses or {})  # idToken -> AuthorizationStatus (default Accepted)
        self.token_groups = dict(token_groups or {})      # idToken -> groupIdToken of the Authorize answer
        self.sign_status = "Accepted"                     # SignCertificate answer
        self.gates: dict[str, asyncio.Event] = {}         # action -> answer only once the event is set
        self.delays: dict[str, float] = {}                # action -> answer delay in seconds
        self.history: list = []                           # every Session, oldest first
        self.registered = registered      # None = accept any station id
        self.passwords = passwords        # None = no auth; {cs_id: password}
        self.ssl_context = ssl_context
        self.sessions: dict[str, Session] = {}
        self.port: Optional[int] = None
        self._server = None

    @property
    def url(self) -> str:
        scheme = "wss" if self.ssl_context else "ws"
        return f"{scheme}://127.0.0.1:{self.port}/ocpp"

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await serve(self._handler, host, port, subprotocols=[SUBPROTOCOL],
                                   process_request=self._check, ssl=self.ssl_context, max_queue=64)
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

    def token_status(self, id_token: str) -> str:
        return self.token_statuses.get(id_token, "Accepted")

    async def request(self, cs_id: str, payload, **kw):
        """Send a CSMS -> CS CALL (an ``ocpp.v201.call`` object) on the current session of ``cs_id``."""
        return await self.sessions[cs_id].cs.call(payload, **kw)

    async def data_transfer(self, cs_id, vendor_id, message_id=None, data=None):
        return await self.request(cs_id, call.DataTransfer(vendor_id=vendor_id, message_id=message_id, data=data))

    def _check(self, connection, request):
        cs_id = _cs_id(request.path)
        if self.registered is not None and cs_id not in self.registered:
            return connection.respond(HTTPStatus.NOT_FOUND, "unknown charging station\n")
        if self.passwords is not None:
            auth = request.headers.get("Authorization") or ""
            try:
                scheme, token = auth.split(" ", 1)
                user, pw = base64.b64decode(token).decode().split(":", 1)  # RFC 7617
            except ValueError:
                user = pw = scheme = None
            if scheme != "Basic" or user != cs_id or self.passwords.get(cs_id) != pw:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "bad credentials\n")
        return None

    async def _handler(self, ws) -> None:
        cs_id = _cs_id(ws.request.path)
        s = Session(cs_id, ws.subprotocol, ws.request.headers.get("Authorization"))
        s.peercert = ws.transport.get_extra_info("peercert") if self.ssl_context else None
        self.sessions[cs_id] = s
        self.history.append(s)
        cs = PeerCS(cs_id, TapConnection(ws, s.frames), response_timeout=5)
        cs.session, cs.peer = s, self
        s.cs = cs
        try:
            await cs.start()
        except ConnectionClosed:
            pass


if __name__ == "__main__":
    async def _main() -> None:
        peer = CsmsPeer201()
        await peer.start(port=9000)
        print(f"ocpp201_peer listening on {peer.url}/<stationId>")
        await asyncio.Future()

    asyncio.run(_main())
