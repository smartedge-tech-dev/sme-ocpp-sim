"""OCPP-J WebSocket client transport: URL, subprotocol, Basic Auth, TLS, handshake errors, back-off.

Security profiles:
  0  ws://   no authentication            (not a spec term: "no security")
  1  ws://   HTTP Basic Auth
  2  wss://  server certificate check + HTTP Basic Auth
  3  wss://  server certificate check + TLS client certificate (v0.2 M8)

Profile 3 (OCPP 2.0.1 Part 2 A00.FR.401 - FR.403, Table 18): the Charging Station authenticates itself with its
certificate as the TLS client certificate; Basic Auth is not part of the profile, so no Authorization header is
sent (``password`` must be ``None``; the config refuses a password with profile 3). The 1.6 Security Whitepaper
edition 4 §2.5 defines the same profile (A00.FR.401 / FR.402; no Basic Auth requirement), profiles 1 / 2 with
the Basic Auth username = the charger identity (A00.FR.204 / FR.303) in §2.3 / §2.4.

A CSMS that refuses the client certificate aborts the TLS handshake with an alert; with TLS 1.3 the client only
sees it while reading the HTTP upgrade response. Such failures are classified ``client_cert_rejected``: a
certificate-related alert (:data:`_CLIENT_CERT_ALERTS`), or - with a client certificate configured - the server
closing the connection after the TLS handshake before any HTTP response (:func:`_lost_after_tls`; e.g. Windows
reports no alert at all, only end-of-stream while reading the status line). Any other alert is ``tls``, a reset
while connecting is ``network``.

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
from websockets.exceptions import InvalidHandshake, InvalidMessage, InvalidStatus, InvalidURI, NegotiationError

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
    #: TLS client certificate chain / private key (PEM files; profile 3).
    client_cert_file: Optional[str] = None
    client_key_file: Optional[str] = None
    #: Extra trusted CA certificates (PEM text), e.g. CSMS roots installed with InstallCertificate.
    ca_data: Optional[str] = field(default=None, repr=False)

    @property
    def url(self) -> str:
        return f"{self.csms_url.rstrip('/')}/{quote(self.charger_id, safe='')}"

    @property
    def is_tls(self) -> bool:
        return urlsplit(self.csms_url).scheme.lower() == "wss"

    @property
    def security_profile(self) -> Optional[int]:
        """0 / 1 / 2 / 3, or None for a combination that is no defined profile (wss:// without credentials, a
        client certificate together with Basic Auth or without TLS)."""
        if self.client_cert_file is not None:
            return 3 if self.is_tls and self.password is None else None
        if self.is_tls:
            return 2 if self.password is not None else None
        return 1 if self.password is not None else 0


def basic_auth_header(user: str, password: str) -> str:
    """``Basic base64(user:password)`` per RFC 7617. The user-id may not contain ':'."""
    if ":" in user:
        raise ValueError("Basic Auth user-id (the charger id) must not contain ':' (RFC 7617)")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def derived_security_profile(password: Optional[str], url: str, client_cert: bool = False) -> int:
    """The security profile a charger reports (1.6 SecurityProfile, 2.0.1 SecurityCtrlr.SecurityProfile): 3 with a
    client certificate over wss://, else 2 / 1 with a Basic Auth password over wss:// / ws://; 0 without
    credentials is a simulator-only value (the spec defines profiles 1 / 2 / 3)."""
    tls = url.lower().startswith("wss://")
    if client_cert and tls:
        return 3
    if password is None:
        return 0
    return 2 if tls else 1


def make_ssl_context(cfg: TransportConfig) -> Optional[ssl.SSLContext]:
    """TLS context of a wss:// connection; ``HandshakeError("client_cert")`` when the client certificate or key
    cannot be loaded, ``HandshakeError("tls")`` when ``ca_file`` / ``ca_data`` cannot."""
    if not cfg.is_tls:
        return None
    try:
        ctx = ssl.create_default_context(cafile=cfg.ca_file)
        if cfg.ca_data:
            ctx.load_verify_locations(cadata=cfg.ca_data)
    except (OSError, ssl.SSLError, ValueError) as e:
        raise HandshakeError("tls", f"CA certificates could not be loaded: {e}") from None
    # A00.FR.313 / FR.416: TLS 1.2 or above only.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if cfg.client_cert_file is not None:
        try:
            ctx.load_cert_chain(cfg.client_cert_file, cfg.client_key_file)
        except (OSError, ssl.SSLError) as e:
            raise HandshakeError("client_cert", str(e)) from None
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
    "client_cert_rejected": "CSMS rejected the TLS client certificate (security profile 3)",
    "client_cert": "the TLS client certificate / key could not be loaded (check client_cert_file / client_key_file)",
    "tls_version": "the CSMS offers no TLS version >= 1.2",
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


#: OpenSSL alert reasons a TLS server sends when it refuses (or misses) the client certificate.
_CLIENT_CERT_ALERTS = frozenset({
    "TLSV13_ALERT_CERTIFICATE_REQUIRED", "SSLV3_ALERT_BAD_CERTIFICATE", "SSLV3_ALERT_CERTIFICATE_UNKNOWN",
    "TLSV1_ALERT_UNKNOWN_CA", "SSLV3_ALERT_CERTIFICATE_EXPIRED", "SSLV3_ALERT_CERTIFICATE_REVOKED",
    "SSLV3_ALERT_UNSUPPORTED_CERTIFICATE", "TLSV1_ALERT_ACCESS_DENIED", "TLSV1_ALERT_DECRYPT_ERROR"})
_TLS_VERSION_REASONS = frozenset({"UNSUPPORTED_PROTOCOL", "TLSV1_ALERT_PROTOCOL_VERSION", "WRONG_SSL_VERSION",
                                  "NO_PROTOCOLS_AVAILABLE"})


def _ssl_cause(e: BaseException) -> Optional[ssl.SSLError]:
    """The ``ssl.SSLError`` behind ``e`` (itself or a ``__cause__`` / ``__context__``), if any."""
    seen = 0
    cur: Optional[BaseException] = e
    while cur is not None and seen < 5:
        if isinstance(cur, ssl.SSLError):
            return cur
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return None


def _lost_after_tls(e: BaseException) -> bool:
    """The server closed the connection after the TLS handshake, before any HTTP upgrade response (no SSL error
    was surfaced): with TLS 1.3 a server that refuses the client certificate may just close. A narrow heuristic,
    applied only with a client certificate configured (Windows surfaces nothing else for a refused certificate)."""
    cur: Optional[BaseException] = e.__cause__ or e.__context__
    return isinstance(e, InvalidMessage) and isinstance(cur, (EOFError, ConnectionResetError))


def classify_ssl(e: ssl.SSLError, cfg: TransportConfig) -> HandshakeError:
    if isinstance(e, ssl.SSLCertVerificationError):
        return HandshakeError("tls_verify", e.verify_message or str(e))
    reason = getattr(e, "reason", None) or ""
    if reason in _TLS_VERSION_REASONS:
        return HandshakeError("tls_version", str(e))
    if reason in _CLIENT_CERT_ALERTS:
        return HandshakeError("client_cert_rejected", str(e))
    return HandshakeError("tls", str(e))


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
        ctx = make_ssl_context(cfg)
        ws = await connect(
            cfg.url,
            subprotocols=[cfg.subprotocol],
            additional_headers=headers,
            ssl=ctx,
            open_timeout=cfg.open_timeout,
            ping_interval=cfg.ping_interval,
            logger=ws_log,
        )
    except HandshakeError:
        raise
    except InvalidStatus as e:
        status = e.response.status_code
        raise HandshakeError(classify_status(status), status=status) from None
    except NegotiationError as e:
        raise HandshakeError("subprotocol", str(e)) from None
    except InvalidURI as e:
        raise HandshakeError("bad_url", str(e)) from None
    except InvalidHandshake as e:
        cause = _ssl_cause(e)
        if cause is not None:
            raise classify_ssl(cause, cfg) from None
        if cfg.client_cert_file is not None and _lost_after_tls(e):
            raise HandshakeError("client_cert_rejected", str(e)) from None
        raise HandshakeError("handshake", str(e)) from None
    except ssl.SSLError as e:
        raise classify_ssl(e, cfg) from None
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


async def wait_event(event: asyncio.Event, timeout: Optional[float]) -> bool:
    """Wait until ``event`` is set or ``timeout`` seconds pass (None = no limit). True if it was set.

    Uses ``asyncio.timeout`` rather than ``asyncio.wait_for``: on Python 3.11, ``wait_for`` can
    swallow a cancellation that arrives while the awaited event completes (fixed in 3.12), which
    left loops running after their session had been cancelled.
    """
    try:
        async with asyncio.timeout(timeout):
            await event.wait()
        return True
    except TimeoutError:
        return event.is_set()


async def wait_or_stop(stop: asyncio.Event, delay: float) -> bool:
    """Sleep ``delay`` seconds unless ``stop`` is set first. Returns True if stopped."""
    return await wait_event(stop, delay)
