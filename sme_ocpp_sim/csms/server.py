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

Dependencies: ``rpc``, ``protocol.<version>.models / codec`` (through ``handlers``) and ``framelog`` only.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..framelog import FrameLog
from ..rpc.connection import FrameInfo, RpcConnection
from ..rpc.errors import CallError
from ..rpc.server import Handshake, OcppServer, Reject, parse_basic_auth
from .handlers import BY_SUBPROTOCOL, SPECS, build_handlers, check_outbound
from .plan import PlanRunner, matching_items
from .session import CpSession

log = logging.getLogger(__name__)

MAX_REJECTIONS = 50


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
        self._tx_seq = 0
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
        self._tx_seq += 1
        return self._tx_seq

    # ------------------------------------------------------------ handshake

    def _select(self, offered: Sequence[str]) -> Optional[str]:
        enabled = {SPECS[v].subprotocol for v in self.cfg.versions if v in SPECS}
        return next((s for s in offered if s in enabled), None)

    def _check(self, hs: Handshake) -> None:
        auth = self.cfg.auth
        if not auth.basic:
            return
        creds = parse_basic_auth(hs.authorization)
        known = {c.id: c.password for c in auth.credentials}
        if hs.station_id not in known:
            raise Reject(401, "unknown charging station")
        if creds is None or creds[0] != hs.station_id or not hmac.compare_digest(
                creds[1].encode("utf-8"), known[hs.station_id].encode("utf-8")):
            raise Reject(401, "bad credentials")

    def _rejected(self, hs: Handshake, status: int, reason: str) -> None:
        self.rejections.append({"ts": self._clock(), "id": hs.station_id, "status": status, "reason": reason,
                                "remote": hs.remote})
        log.info("Mini CSMS rejected %r (%s): %s", hs.station_id, status, reason)

    # ------------------------------------------------------------ connection

    async def _on_connection(self, ws: Any, hs: Handshake, subprotocol: str) -> None:
        spec = BY_SUBPROTOCOL[subprotocol]
        cp_id = hs.station_id
        old = self._conns.pop(cp_id, None)
        if old is not None:
            old.runner.cancel()
            await old.rpc.close()
        session = CpSession(cp_id, spec.version, subprotocol, remote=hs.remote,
                            basic_auth=hs.authorization is not None, clock=self._clock)
        self.sessions[cp_id] = session
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
            runner.trigger("connect")
            await rpc.run()
        finally:
            runner.cancel()
            session.closed()
            if self._conns.get(cp_id) is conn:
                del self._conns[cp_id]
            log.info("Mini CSMS: %s disconnected", cp_id)

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
        entry: Dict[str, Any] = {"ts": self._clock(), "source": source, "action": action, "payload": payload}
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
