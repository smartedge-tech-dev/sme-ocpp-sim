"""Mini CSMS (plan M9): a simple OCPP 1.6J / 2.0.1 CSMS in the simulator process.

:class:`MiniCsms` listens on ``mini_csms.host:port`` (``rpc.OcppServer``), negotiates the subprotocol among
``mini_csms.versions`` (the first one the station offers that is enabled; none -> the connection is closed),
optionally checks HTTP Basic Auth (Security Profile 1; no TLS server: profiles 2 / 3 are out of scope) and runs one
``rpc.RpcConnection`` per station with the version's answers (``handlers.py``). What it learns is kept per
connection in a :class:`CpSession`; the newest session of every station id stays visible after it disconnects.
A station that connects again while its old connection is still open replaces it (the old one is closed).

Outbound CALLs (:meth:`MiniCsms.call`, the Web API's JSON editor and the plan) are checked against the version's
request model first (strict: unknown fields are errors too), so a typo never reaches the station; the answer is
checked against the response model and a mismatch is reported as ``warning`` next to the result.

Frames go to the Mini CSMS's own :class:`FrameLog` (``charger`` = station id, direction seen from the CSMS), not to
the simulator's charger frame log. Configuration changes other than ``host`` / ``port`` apply at once
(:meth:`update`); the plan of an open connection is fixed when it connects.

Limits (no rate limiting beyond these): at most :data:`MAX_CONNECTIONS` open connections (a further station gets
HTTP 503 before the upgrade; a reconnecting id does not count twice), at most :data:`MAX_SESSIONS` remembered
stations (the oldest disconnected one is forgotten first). Basic Auth failures answer one generic 401 with
``WWW-Authenticate`` whatever the cause (unknown id, wrong password, malformed header), so the response does not
tell which station ids exist; the operator's rejection list keeps the cause. Secrets in the CALLs the CSMS sends
(AuthorizationKey, BasicAuthPassword, ...) are masked in its frame log and recorded calls (``framelog.mask_*``).

Dependencies: ``rpc``, ``protocol.<version>.models / codec`` (through ``handlers``) and ``framelog`` only.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..framelog import FrameLog, mask_payload
from ..rpc.connection import FrameInfo, RpcConnection
from ..rpc.errors import CallError
from ..rpc.server import Handshake, OcppServer, Reject, parse_basic_auth
from .handlers import BY_SUBPROTOCOL, SPECS, build_handlers, check_outbound
from .plan import PlanRunner, matching_items
from .session import CpSession

log = logging.getLogger(__name__)

MAX_REJECTIONS = 50
#: Open WebSocket connections at most (further handshakes: HTTP 503).
MAX_CONNECTIONS = 500
#: Remembered stations at most (connected ones are never forgotten).
MAX_SESSIONS = 200
#: 1.6 transactionId is an ``integer`` (int32 for most CSMS / stations).
_INT32_MAX = 2 ** 31 - 1
BASIC_REALM = "SME OCPPSim Mini CSMS"
_UNAUTHORIZED = "unauthorized"


class _Conn:
    def __init__(self, session: CpSession, rpc: RpcConnection, runner: PlanRunner) -> None:
        self.session, self.rpc, self.runner = session, rpc, runner


class MiniCsms:
    def __init__(self, cfg: Any, *, clock: Callable[[], float] = time.time) -> None:
        """``cfg`` is a ``config.MiniCsmsConfig`` (duck-typed; read on every use, see :meth:`update`)."""
        self.cfg = cfg
        self._clock = clock
        self.frame_log = FrameLog(cfg.frame_log_size, clock=clock)
        self.sessions: Dict[str, CpSession] = {}
        self._conns: Dict[str, _Conn] = {}
        self.rejections: deque = deque(maxlen=MAX_REJECTIONS)
        # Seeded from the clock (seconds, below 1e9) so ids issued after a restart do not repeat the last run's.
        self._tx_seq = int(clock()) % 1_000_000_000
        self._server: Optional[OcppServer] = None
        self.port: Optional[int] = None

    # ------------------------------------------------------------ lifecycle

    @property
    def listen_key(self) -> tuple:
        return (self.cfg.host, self.cfg.port)

    @property
    def running(self) -> bool:
        return self._server is not None

    def update(self, cfg: Any) -> None:
        """Apply a changed config with the same ``host`` / ``port`` (no restart, connections stay)."""
        self.cfg = cfg
        self.frame_log.resize(cfg.frame_log_size)

    async def start(self) -> int:
        """Listen; returns the bound port. ``OSError`` when the port cannot be bound."""
        server = OcppServer(self._on_connection, select=self._select, check=self._check, on_reject=self._rejected)
        self.port = await server.start(self.cfg.host, self.cfg.port)
        self._server = server
        log.info("Mini CSMS listening on ws://%s:%s", self.cfg.host, self.port)
        return self.port

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.stop()
        for c in list(self._conns.values()):
            c.runner.cancel()

    def next_tx_id(self) -> int:
        self._tx_seq = self._tx_seq + 1 if self._tx_seq < _INT32_MAX else 1
        return self._tx_seq

    # ------------------------------------------------------------ handshake

    def _select(self, offered: Sequence[str]) -> Optional[str]:
        enabled = {SPECS[v].subprotocol for v in self.cfg.versions if v in SPECS}
        return next((s for s in offered if s in enabled), None)

    def _check(self, hs: Handshake) -> None:
        if len(self._conns) >= MAX_CONNECTIONS and hs.station_id not in self._conns:
            raise Reject(503, f"too many connections (at most {MAX_CONNECTIONS})")
        auth = self.cfg.auth
        if not auth.basic:
            return
        creds = parse_basic_auth(hs.authorization)
        known = {c.id: c.password for c in auth.credentials}
        if creds is None:
            detail = "no or malformed Basic Authorization header"
        elif hs.station_id not in known:
            detail = "unknown charging station"
        elif creds[0] != hs.station_id or not hmac.compare_digest(
                creds[1].encode("utf-8"), known[hs.station_id].encode("utf-8")):
            detail = "bad credentials"
        else:
            return
        raise Reject(401, _UNAUTHORIZED, headers={"WWW-Authenticate": f'Basic realm="{BASIC_REALM}"'},
                     detail=detail)

    def _rejected(self, hs: Handshake, status: Optional[int], reason: str) -> None:
        self.rejections.append({"ts": self._clock(), "id": hs.station_id, "status": status, "reason": reason,
                                "remote": hs.remote})
        log.info("Mini CSMS rejected %r (%s): %s", hs.station_id, status, reason)

    # ------------------------------------------------------------ connection

    async def _on_connection(self, ws: Any, hs: Handshake, subprotocol: str) -> None:
        spec = BY_SUBPROTOCOL[subprotocol]
        cp_id = hs.station_id
        # Everything up to ``self._conns[cp_id] = conn`` runs without an await, so two handshakes of the same id
        # cannot interleave here: the newest connection is registered first, the one it replaces closed after.
        old = self._conns.get(cp_id)
        session = CpSession(cp_id, spec.version, subprotocol, remote=hs.remote,
                            basic_auth=hs.authorization is not None, clock=self._clock)
        self._remember(session)
        frame_cb = self.frame_log.callback(cp_id)

        def on_frame(info: FrameInfo) -> None:
            frame_cb(info)
            # The plan's "boot" trigger fires once the Accepted answer has been handed to the socket, so a
            # planned CALL never overtakes the BootNotification CALLRESULT.
            if (info.direction == "tx" and info.action == "BootNotification" and info.message_type == 3
                    and session.boot_status == "Accepted"):
                conn.runner.trigger("boot")

        rpc = RpcConnection(ws, error_codes=spec.errors, call_timeout=self.cfg.call_timeout_s, on_frame=on_frame,
                            name=f"csms:{cp_id}")
        rpc.handlers = build_handlers(spec, session, self)
        items = matching_items(self.cfg.plan, cp_id, spec.version)
        runner = PlanRunner(items, lambda a, p: self._send(conn, a, p, None, "plan"), session.plan, self._clock)
        conn = _Conn(session, rpc, runner)
        self._conns[cp_id] = conn
        log.info("Mini CSMS: %s connected (OCPP %s)", cp_id, spec.version)
        try:
            if old is not None:
                old.runner.cancel()
                await old.rpc.close()
            runner.trigger("connect")
            await rpc.run()
        finally:
            runner.cancel()
            session.closed()
            if self._conns.get(cp_id) is conn:
                del self._conns[cp_id]
            log.info("Mini CSMS: %s disconnected", cp_id)

    def _remember(self, session: CpSession) -> None:
        """Make ``session`` the station's newest (moved to the end) and forget the oldest disconnected stations
        beyond :data:`MAX_SESSIONS`."""
        self.sessions.pop(session.cp_id, None)
        self.sessions[session.cp_id] = session
        if len(self.sessions) > MAX_SESSIONS:
            for cp_id in [k for k, s in self.sessions.items() if not s.connected][:len(self.sessions) - MAX_SESSIONS]:
                del self.sessions[cp_id]

    # ------------------------------------------------------------ outbound CALLs

    async def call(self, cp_id: str, action: str, payload: Any, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send ``action`` to station ``cp_id`` and wait for the answer.

        Returns ``{"ok": True, "result": {...}}`` (plus ``warning`` when the answer does not match the response
        schema) or ``{"ok": False, "error": {"code", "description", "details"}}`` for a CALLERROR. Raises
        ``KeyError`` (no such station), ``ConnectionError`` (not connected), ``ValueError`` (not a CSMS -> CP
        action of the station's version, or the payload does not match its schema) and ``TimeoutError``."""
        session = self.sessions[cp_id]
        check_outbound(session.version, action, payload)  # before the connection check: the editor's error path
        conn = self._conns.get(cp_id)
        if conn is None:
            raise ConnectionError(f"station {cp_id!r} is not connected")
        return await self._send(conn, action, payload, timeout, "manual")

    async def _send(self, conn: _Conn, action: str, payload: Any, timeout: Optional[float],
                    source: str) -> Dict[str, Any]:
        session = conn.session
        check_outbound(session.version, action, payload)
        entry: Dict[str, Any] = {"ts": self._clock(), "source": source, "action": action,
                                 "payload": mask_payload(action, payload)}
        session.record_call(entry)
        try:
            result = await conn.rpc.call(action, payload, timeout=timeout)
        except CallError as e:
            out: Dict[str, Any] = {"ok": False, "error": {"code": e.code, "description": e.description,
                                                         "details": e.details}}
        except (TimeoutError, ConnectionError) as e:
            entry["error"] = type(e).__name__
            raise
        else:
            out = {"ok": True, "result": result}
            try:
                SPECS[session.version].codec.parse(action, result, "response", strict=False)
            except CallError as e:
                out["warning"] = f"response does not match the schema: {e.code}: {e.description}"
        entry.update(out)
        return out

    # ------------------------------------------------------------ views

    def connected(self, cp_id: str) -> bool:
        return cp_id in self._conns

    def actions(self, version: str) -> List[str]:
        return list(SPECS[version].csms_actions)

    def transactions(self) -> List[Dict[str, Any]]:
        return [{"charger": s.cp_id, **tx} for s in self.sessions.values() for tx in s.transactions.values()]

    def snapshot(self) -> Dict[str, Any]:
        return {"running": self.running, "host": self.cfg.host, "port": self.port if self.running else self.cfg.port,
                "versions": list(self.cfg.versions), "basic_auth": self.cfg.auth.basic,
                "boot_status": self.cfg.boot.status, "plan_items": len(self.cfg.plan),
                "chargers": [s.summary() for s in self.sessions.values()],
                "rejections": list(self.rejections), "frame_seq": self.frame_log.last_seq}
