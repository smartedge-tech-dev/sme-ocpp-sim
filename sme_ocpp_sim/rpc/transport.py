"""OCPP-J WebSocket client transport: URL, subprotocol, Basic Auth, TLS, handshake errors, back-off.

Security profiles (v0.1):
  0  ws://   no authentication            (not a spec term: "no security")
  1  ws://   HTTP Basic Auth
  2  wss://  server certificate check + HTTP Basic Auth
Profile 3 (client certificate, mTLS) comes in v0.2.

The Basic Auth password never appears in logs, reprs or error messages.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
import ssl
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import quote, urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidHandshake, InvalidStatus, InvalidURI, NegotiationError

log = logging.getLogger(__name__)


class _RedactAuthorization(logging.Filter):
    """websockets logs every handshake header at DEBUG; Basic credentials are only base64."""

    _HEADER = re.compile(r"(?i)(authorization:\s*\S+\s+)\S+")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "uthorization" in msg:
            record.msg, record.args = self._HEADER.sub(r"\g<1>[redacted]", msg), ()
        return True


# Logger handed to websockets' client connections (instead of "websockets.client").
ws_log = logging.getLogger(__name__ + ".ws")
ws_log.addFilter(_RedactAuthorization())


@dataclass
class TransportConfig:
    csms_url: str                       # ws://host:port/path or wss://...; the charger id is appended
    charger_id: str
    subprotocol: str = "ocpp1.6"
    password: Optional[str] = field(default=None, repr=False)  # Basic Auth (profile 1 / 2)
    ca_file: Optional[str] = None       # extra CA bundle (PEM) for wss://
    verify_tls: bool = True             # False = skip certificate checks (testing only, UI warns)
    require_subprotocol: bool = True    # False = keep going when the CSMS echoes no subprotocol
    open_timeout: float = 10.0
    ping_interval: Optional[float] = 20.0

    @property
    def url(self) -> str:
        return f"{self.csms_url.rstrip('/')}/{quote(self.charger_id, safe='')}"

    @property
    def is_tls(self) -> bool:
        return urlsplit(self.csms_url).scheme.lower() == "wss"

    @property
    def security_profile(self) -> Optional[int]:
        """0 / 1 / 2, or None for wss:// without Basic Auth (not a defined profile)."""
        if self.is_tls:
            return 2 if self.password is not None else None
        return 1 if self.password is not None else 0


def basic_auth_header(user: str, password: str) -> str:
    """``Basic base64(user:password)`` per RFC 7617. The user-id may not contain ':'."""
    if ":" in user:
        raise ValueError("Basic Auth user-id (the charger id) must not contain ':' (RFC 7617)")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def make_ssl_context(cfg: TransportConfig) -> Optional[ssl.SSLContext]:
    if not cfg.is_tls:
        return None
    ctx = ssl.create_default_context(cafile=cfg.ca_file)
    if not cfg.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------------- handshake errors

# kind -> human readable hint (English; the UI translates by kind)
HINTS = {
    "bad_request": "CSMS rejected the handshake (HTTP 400); often a missing or unsupported subprotocol",
    "unauthorized": "CSMS rejected the credentials (HTTP 401); check the Basic Auth password",
    "forbidden": "CSMS refused the charger (HTTP 403)",
    "not_found": ("HTTP 404: charger id not registered at the CSMS, or subprotocol not supported "
                  "(e.g. SteVe answers 404 for both; the charger cannot tell them apart)"),
    "http_error": "CSMS rejected the handshake",
    "subprotocol": "CSMS did not accept the OCPP subprotocol",
    "tls_verify": "TLS certificate verification failed (check ca_file / hostname)",
    "tls": "TLS error",
    "bad_url": "invalid CSMS URL",
    "timeout": "connection timed out",
    "network": "network error (CSMS unreachable?)",
    "handshake": "WebSocket handshake failed",
}

_STATUS_KINDS = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found"}


class HandshakeError(ConnectionError):
    def __init__(self, kind: str, detail: str = "", status: Optional[int] = None) -> None:
        self.kind = kind
        self.status = status
        self.detail = detail
        hint = HINTS.get(kind, kind)
        msg = f"{hint} [HTTP {status}]" if status and kind == "http_error" else hint
        super().__init__(f"{msg}: {detail}" if detail else msg)


def classify_status(status: int) -> str:
    return _STATUS_KINDS.get(status, "http_error")


async def open_connection(cfg: TransportConfig) -> ClientConnection:
    """Open the WebSocket to ``cfg.url``; raise :class:`HandshakeError` on any failure."""
    headers = {}
    if cfg.password is not None:
        headers["Authorization"] = basic_auth_header(cfg.charger_id, cfg.password)
    log.info("connecting %s (profile %s, subprotocol %s)", cfg.url, cfg.security_profile,
             cfg.subprotocol)
    if cfg.is_tls and not cfg.verify_tls:
        log.warning("%s: TLS certificate verification is OFF (testing only)", cfg.charger_id)
    try:
        ws = await connect(
            cfg.url,
            subprotocols=[cfg.subprotocol],
            additional_headers=headers,
            ssl=make_ssl_context(cfg),
            open_timeout=cfg.open_timeout,
            ping_interval=cfg.ping_interval,
            logger=ws_log,
        )
    except InvalidStatus as e:
        status = e.response.status_code
        raise HandshakeError(classify_status(status), status=status) from None
    except NegotiationError as e:
        raise HandshakeError("subprotocol", str(e)) from None
    except InvalidURI as e:
        raise HandshakeError("bad_url", str(e)) from None
    except InvalidHandshake as e:
        raise HandshakeError("handshake", str(e)) from None
    except ssl.SSLCertVerificationError as e:
        raise HandshakeError("tls_verify", e.verify_message or str(e)) from None
    except ssl.SSLError as e:
        raise HandshakeError("tls", str(e)) from None
    except TimeoutError:
        raise HandshakeError("timeout") from None
    except OSError as e:
        raise HandshakeError("network", e.strerror or str(e)) from None

    if ws.subprotocol != cfg.subprotocol:
        if cfg.require_subprotocol:
            await ws.close()
            raise HandshakeError("subprotocol", f"CSMS answered {ws.subprotocol!r}")
        log.warning("%s: CSMS echoed no subprotocol; continuing (require_subprotocol=false)",
                    cfg.charger_id)
    return ws


# ---------------------------------------------------------------------- reconnect back-off

class Backoff:
    """Exponential back-off with jitter: delay n is ``min(maximum, initial * factor**n)``
    reduced by a random share of up to ``jitter`` (0..1). ``rng`` is injectable for tests."""

    def __init__(self, initial: float = 1.0, factor: float = 2.0, maximum: float = 60.0,
                 jitter: float = 0.5, rng: Callable[[], float] = random.random) -> None:
        if initial <= 0 or factor < 1 or maximum < initial or not 0 <= jitter <= 1:
            raise ValueError("invalid back-off parameters")
        self.initial, self.factor, self.maximum, self.jitter = initial, factor, maximum, jitter
        self._rng = rng
        self.attempt = 0

    def next(self) -> float:
        base = min(self.maximum, self.initial * self.factor ** self.attempt)
        if base < self.maximum:
            self.attempt += 1
        return base * (1 - self.jitter * self._rng())

    def reset(self) -> None:
        self.attempt = 0


async def wait_or_stop(stop: asyncio.Event, delay: float) -> bool:
    """Sleep ``delay`` seconds unless ``stop`` is set first. Returns True if stopped."""
    try:
        await asyncio.wait_for(stop.wait(), delay)
        return True
    except TimeoutError:
        return stop.is_set()
