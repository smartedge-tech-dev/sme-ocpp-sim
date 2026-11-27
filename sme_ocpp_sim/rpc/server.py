"""OCPP-J WebSocket server transport (the Mini CSMS side; ``websockets`` stays inside ``rpc``).

The station id is the last path segment of the URL (``ws://host:port/<anything>/<stationId>``, percent-decoded).

Handshake (in this order):

* no station id in the path -> HTTP 404;
* ``check(handshake)`` raises :class:`Reject` -> that HTTP status (e.g. 401 for wrong Basic Auth credentials);
* subprotocol: ``select(offered)`` picks one of the subprotocols the client offered. When it picks none, the
  handshake completes *without* a ``Sec-WebSocket-Protocol`` header and the server closes the connection at once.
  This is what OCPP-J 1.6 §3.2 and OCPP 2.0.1 Part 4 §3.2 require: a server that agrees to none of the offered
  subprotocols MUST complete the handshake without the header and then immediately close the connection. Neither
  specifies the close code; we send 1002 (RFC 6455 protocol error). The operator's view gets the close as a
  rejection with ``status`` ``None`` (it is not an HTTP status).

Compression: ``websockets`` negotiates permessage-deflate by default, which OCPP 2.0.1 Part 4 §3.3 allows (a client
may offer it, the server may accept it); 1.6 says nothing about it and its clients may simply not offer it.

The ``Authorization`` header is never logged by this module; the ``websockets`` server logger gets the same
redaction filter as the client one (``transport._RedactAuthorization``).
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Awaitable, Callable, Optional, Sequence
from urllib.parse import unquote, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .transport import _RedactAuthorization

log = logging.getLogger(__name__)
ws_log = logging.getLogger(__name__ + ".ws")
ws_log.addFilter(_RedactAuthorization())

#: Close code sent when no offered subprotocol is supported (RFC 6455 1002 = protocol error).
CLOSE_NO_SUBPROTOCOL = 1002


@dataclass(frozen=True)
class Handshake:
    path: str
    station_id: str
    offered: tuple                      # subprotocols offered by the client, in its order
    authorization: Optional[str]        # raw header value (never logged)
    remote: Optional[str] = None        # "host:port" of the peer


class Reject(Exception):
    """Raised by a ``check`` callback: refuse the handshake with HTTP ``status``."""

    def __init__(self, status: int, reason: str, *, headers: Optional[dict] = None,
                 detail: Optional[str] = None) -> None:
        """``reason`` is the response body (keep it generic for auth failures); ``detail`` (default ``reason``) is
        what the operator's view (``on_reject``) gets; ``headers`` are added to the response
        (e.g. ``WWW-Authenticate``)."""
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.headers = dict(headers or {})
        self.detail = detail or reason


def station_id_from_path(path: str) -> str:
    """The percent-decoded last segment of the URL path ('' when there is none)."""
    p = urlsplit(path).path.rstrip("/")
    return unquote(p.rsplit("/", 1)[-1]) if "/" in p else unquote(p)


def parse_basic_auth(header: Optional[str]) -> Optional[tuple]:
    """``(user, password)`` of a ``Basic`` Authorization header (RFC 7617), ``None`` if absent or malformed."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token:
        return None
    try:
        text = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    user, sep, password = text.partition(":")
    return (user, password) if sep else None


def _remote(ws: Any) -> Optional[str]:
    addr = getattr(ws, "remote_address", None)
    if isinstance(addr, tuple) and len(addr) >= 2:
        return f"{addr[0]}:{addr[1]}"
    return None


def handshake_of(ws: Any) -> Handshake:
    req = ws.request
    offered = tuple(s.strip() for h in req.headers.get_all("Sec-WebSocket-Protocol") for s in h.split(",")
                    if s.strip())
    return Handshake(req.path, station_id_from_path(req.path), offered, req.headers.get("Authorization"),
                     _remote(ws))


#: ``on_connection(ws, handshake, subprotocol)``: runs for the lifetime of an accepted connection.
OnConnection = Callable[[Any, Handshake, str], Awaitable[None]]
#: ``on_reject(handshake, status, reason)``: a refused handshake (for the operator's view); ``status`` is the HTTP
#: status, or ``None`` for a connection closed after the upgrade (no supported subprotocol).
OnReject = Callable[[Handshake, Optional[int], str], None]


class OcppServer:
    """A WebSocket server for OCPP-J charging stations. ``select(offered) -> subprotocol | None`` and ``check``
    are called per handshake, so a changed configuration applies to the next connection without a restart."""

    def __init__(self, on_connection: OnConnection, *, select: Callable[[Sequence[str]], Optional[str]],
                 check: Optional[Callable[[Handshake], None]] = None, on_reject: Optional[OnReject] = None,
                 ping_interval: Optional[float] = 20.0, max_size: int = 2 ** 20) -> None:
        self._on_connection = on_connection
        self._select = select
        self._check = check
        self._on_reject = on_reject
        self._ping_interval = ping_interval
        self._max_size = max_size
        self._server: Optional[Server] = None
        self.port: Optional[int] = None

    async def start(self, host: str, port: int) -> int:
        """Bind and listen; returns the bound port (``port`` 0 = any free port). ``OSError`` when it cannot bind."""
        self._server = await serve(self._handler, host, port, process_request=self._process_request,
                                   select_subprotocol=lambda _conn, offered: self._select(offered),
                                   ping_interval=self._ping_interval, max_size=self._max_size, logger=ws_log)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        """Stop listening and close every connection (close code 1001)."""
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    def _notify_reject(self, hs: Handshake, status: Optional[int], reason: str) -> None:
        if self._on_reject is not None:
            try:
                self._on_reject(hs, status, reason)
            except Exception:  # noqa: BLE001 - an observer must not break the handshake
                log.exception("on_reject failed")

    def _reject(self, connection: ServerConnection, hs: Handshake, status: int, reason: str,
                headers: Optional[dict] = None, detail: Optional[str] = None):
        self._notify_reject(hs, status, detail or reason)
        response = connection.respond(HTTPStatus(status), reason + "\n")
        for k, v in (headers or {}).items():
            response.headers[k] = v
        return response

    def _process_request(self, connection: ServerConnection, request: Any):
        hs = Handshake(request.path, station_id_from_path(request.path), (), request.headers.get("Authorization"),
                       _remote(connection))
        if not hs.station_id:
            return self._reject(connection, hs, 404, "no charging station id in the URL path")
        if self._check is not None:
            try:
                self._check(hs)
            except Reject as e:
                return self._reject(connection, hs, e.status, e.reason, e.headers, e.detail)
        return None

    async def _handler(self, ws: ServerConnection) -> None:
        hs = handshake_of(ws)
        if not ws.subprotocol:
            reason = f"no supported subprotocol offered ({', '.join(hs.offered) or 'none'})"
            self._notify_reject(hs, None, reason)
            await ws.close(CLOSE_NO_SUBPROTOCOL, "unsupported subprotocol")
            return
        try:
            await self._on_connection(ws, hs, ws.subprotocol)
        except ConnectionClosed:
            pass
