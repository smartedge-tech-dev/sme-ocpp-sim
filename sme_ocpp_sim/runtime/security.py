"""Version-neutral security support (v0.2 M8): security event log / queue, CA certificate store, CSR and the
Charging Station (client) certificate.

1.6 Security (the OCPP 1.6 Security Whitepaper edition 2+, whose messages the ``ocpp`` library's 1.6 schemas
carry) and 2.0.1 (Part 2 A02 / A03 / A04, M03 / M04 / M05) define the same mechanics under different wire names.
Only the mechanics live here; the wire messages, the name of each event and every response belong to the version
station. This module imports nothing of the shell or of a version package (like ``runtime.maintenance``).

Spec source: OCPP 2.0.1 Edition 4 Part 2 (A00 Security Profile 3 / Certificate Properties, A02, A04, M03 - M05)
and Appendices CSV ``security_events.csv`` (which events are critical). The 1.6 Security Whitepaper is not in the
local spec folder: the 1.6 side follows the ``ocpp`` library's 1.6 security schemas and assumes the 2.0.1 rules
(UNVERIFIED against the whitepaper text).

Interpretations (not defined by the spec)
-----------------------------------------
- Security log (A04.FR.04): every event is kept in a rolling log of :data:`LOG_MAX` entries. A critical event
  (``security_events.csv`` "Critical: Yes") is always queued for the CSMS and survives disconnects (A04.FR.01 /
  FR.02); a non-critical one is queued only when it happens while connected (it is logged either way). Queued
  events go out in order, one at a time, once the boot is accepted; a lost connection keeps the head event for the
  next session, a timeout retries it after :data:`RETRY_S`, a CALLERROR or invalid answer drops it (the CSMS
  cannot take it; logged). At most :data:`QUEUE_MAX` events are queued: when the queue is full the oldest
  non-critical event is dropped, and only when every queued event is critical the oldest critical one (it stays in
  the security log either way).
- CSR (A02.FR.02 / FR.03): a new EC P-256 key pair (A00.FR.503: at least 224 bits) for every request; the
  subject is CN = the charger's ``serial`` (A00.FR.511: the unique serial number; the charger id when no serial
  is configured) and O = the organization name (A00.FR.509: 2.0.1 SecurityCtrlr.OrganizationName, 1.6 CpoName),
  signed with SHA-256. The keys of the last :data:`PENDING_MAX` CSRs are kept until a CertificateSigned uses
  one or the CSMS answers the SignCertificate with ``Rejected`` (a CALL timeout or CALLERROR keeps the key: the
  CSMS may still sign it).
- CertificateSigned (A02.FR.06 / FR.07): ``Rejected`` when the chain is no PEM certificate, when no CSR is
  outstanding, when the first certificate's public key is no outstanding CSR's or when "now" is outside its
  validity period. Whether the chain belongs to the CSO hierarchy is NOT checked (no chain validation beyond
  what TLS does; the CSMS checks the certificate on the next TLS handshake). An accepted chain becomes the
  client certificate; the station reconnects with it (A02.FR.08).
- Renewed certificate fallback (A02.FR.28 / FR.10 / FR.29): until a session with the renewed certificate has
  been accepted, the previous certificate is kept. If the next connection attempt fails for a reason that can be
  attributed to the client certificate (A02.FR.28: the CSMS refused it in the TLS handshake, it cannot be loaded,
  or HTTP 401 / 403 - with profile 3 the certificate is the only credential), the renewed one is discarded, the
  previous one is used again and DiscardedRenewedClientCertificate is raised; any other failure (network, timeout,
  the CSMS certificate, another TLS error) keeps the renewed one for the next attempt. Once a boot is accepted
  with the renewed one, the previous one is discarded. A second renewal before that replaces the first renewed
  certificate and keeps the original previous one.
- Certificates and keys live in memory and in a private temporary directory (``ssl`` loads a client
  certificate only from files; key files are created with mode 0600). A superseded or discarded pair is deleted at
  once and the directory when the charger stops (:meth:`Security.close`); nothing is written back to the YAML, so
  stopping the charger reverts to the configured ``client_cert_file`` / ``client_key_file`` and a restart also
  starts without installed CA certificates.
- InstallCertificate (M05): ``Rejected`` for text that is not exactly one PEM certificate, one outside its
  validity period (M05.FR.07) or one that is no CA certificate (every install kind is a root certificate, and an
  installed CSMS root becomes a TLS trust anchor: BasicConstraints CA=true is required), and when
  :attr:`Security.max_certificates` CA certificates are installed already
  (M05.FR.06); a certificate installed again is ``Accepted`` and stored once. Installed CSMS root certificates are
  trusted for the TLS server check of the next connection, in addition to the configured ``ca_file``.
  AdditionalRootCertificateCheck is not supported (``false``): several CSMS roots may be installed.
- Certificate hash data (2.0.1 §2.6, M03): SHA-256 of the DER issuer name, of the issuer's public key bits
  (``subjectPublicKey`` BIT STRING value) and the serial number as lower-case hex without leading zeroes.
  The issuer key is the key of the installed certificate whose subject is the issuer (the certificate itself
  when it is self-signed). When the issuer is not installed (e.g. an intermediate-signed client certificate) its
  key is unknown and the certificate's own key stands in, so such an issuerKeyHash differs from the CSMS's
  (AuthorityKeyIdentifier cannot replace it: it is no SHA-256 of the issuer key). A
  DeleteCertificate matches with the algorithm it names; the serial number is compared case-insensitively.
- DeleteCertificate of the client certificate is ``Failed`` (M04.FR.06); CertificateEntries counts the
  installed CA certificates plus the client certificate.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Sequence, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ..protocol.base import CertificateHashData, CertStatus, CertUse, SecurityEvent, SecurityEventKind

log = logging.getLogger(__name__)

K = SecurityEventKind
#: Events marked "Critical: Yes" in OCPP 2.0.1 Part 2 Appendices ``security_events.csv``.
CRITICAL = frozenset({K.FIRMWARE_UPDATED, K.STARTUP_OF_THE_DEVICE, K.RESET_OR_REBOOT, K.INVALID_CSMS_CERTIFICATE,
                      K.INVALID_CHARGING_STATION_CERTIFICATE, K.DISCARDED_RENEWED_CLIENT_CERTIFICATE,
                      K.INVALID_TLS_VERSION})
LOG_MAX = 1000
QUEUE_MAX = 1000
RETRY_S = 10.0
#: Outstanding CSRs whose keys are kept (see the module docstring).
PENDING_MAX = 3
#: Default capacity of the CA certificate store (2.0.1 CertificateEntries maxLimit, 1.6 CertificateStoreMaxLength).
CERT_MAX = 20
TECH_INFO_MAX = 255  # SecurityEventNotification techInfo maxLength (both versions)

_HASHES = {"SHA256": hashlib.sha256, "SHA384": hashlib.sha384, "SHA512": hashlib.sha512}

#: Connection failure kind (``rpc.transport``) -> event (2.0.1 A00.FR.310 / FR.316: InvalidCsmsCertificate /
#: InvalidTLSVersion; a refused Basic Auth password or client certificate: FailedToAuthenticateAtCsms).
_CONNECT_EVENTS = {"tls_verify": K.INVALID_CSMS_CERTIFICATE, "tls_version": K.INVALID_TLS_VERSION,
                   "unauthorized": K.FAILED_TO_AUTHENTICATE_AT_CSMS,
                   "client_cert_rejected": K.FAILED_TO_AUTHENTICATE_AT_CSMS}
#: Failures attributable to the client certificate (A02.FR.28): they discard an unconfirmed renewed one. Not:
#: CSMS unreachable, timeouts, bad url, the CSMS certificate (tls_verify) or other TLS / handshake errors.
_FALLBACK_KINDS = frozenset({"client_cert_rejected", "client_cert", "unauthorized", "forbidden"})


# ---------------------------------------------------------------------- certificate helpers

def load_chain(pem: str) -> List[x509.Certificate]:
    """Every certificate of a PEM text (``ValueError`` when there is none or one is malformed)."""
    certs = x509.load_pem_x509_certificates(pem.encode("ascii", "strict"))
    if not certs:
        raise ValueError("no certificate")
    return certs


def _key_bits(cert: x509.Certificate) -> bytes:
    """The ``subjectPublicKey`` BIT STRING value (without the unused-bits octet) of ``cert``."""
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

    def tlv(i: int) -> Tuple[int, int]:  # (value start, value end) of the DER element at ``i``
        n, i = spki[i + 1], i + 2
        if n & 0x80:
            k = n & 0x7F
            n, i = int.from_bytes(spki[i:i + k], "big"), i + k
        return i, i + n

    start, _ = tlv(0)          # SubjectPublicKeyInfo SEQUENCE
    _, alg_end = tlv(start)    # AlgorithmIdentifier
    bits, end = tlv(alg_end)   # subjectPublicKey BIT STRING
    return spki[bits + 1:end]


def hash_data(cert: x509.Certificate, issuer: Optional[x509.Certificate] = None,
              algorithm: str = "SHA256") -> CertificateHashData:
    """CertificateHashData of ``cert``; ``issuer`` supplies the issuer key (``None``: ``cert``'s own key, right
    only for a self-signed certificate - see the module docstring)."""
    h = _HASHES[algorithm]
    key_of = issuer if issuer is not None else cert
    return CertificateHashData(algorithm, h(cert.issuer.public_bytes()).hexdigest(), h(_key_bits(key_of)).hexdigest(),
                               format(cert.serial_number, "x"))


def _same(a: CertificateHashData, b: CertificateHashData) -> bool:
    return (a.issuer_name_hash.lower(), a.issuer_key_hash.lower(), a.serial_number.lower().lstrip("0")) == (
        b.issuer_name_hash.lower(), b.issuer_key_hash.lower(), b.serial_number.lower().lstrip("0"))


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _valid_at(cert: x509.Certificate, now: dt.datetime) -> bool:
    return cert.not_valid_before_utc <= now <= cert.not_valid_after_utc


def subject_text(cert: x509.Certificate) -> str:
    return cert.subject.rfc4514_string()


def make_csr(common_name: str, organization: Optional[str]) -> Tuple[ec.EllipticCurvePrivateKey, str]:
    """A new key pair and its PEM CSR (see the module docstring)."""
    key = ec.generate_private_key(ec.SECP256R1())
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    if organization:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization))
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attrs)).sign(key, hashes.SHA256())
    return key, csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


# ---------------------------------------------------------------------- the security state of one charger

class Security:
    """One charger's security log / event queue, CA store and client certificate (see the module docstring).

    ``clock`` is the wall clock (UTC). ``client_cert_file`` / ``client_key_file``: the configured client
    certificate (Security Profile 3), ``None`` without one."""

    def __init__(self, *, clock: Callable[[], dt.datetime], client_cert_file: Optional[str] = None,
                 client_key_file: Optional[str] = None, max_certificates: int = CERT_MAX) -> None:
        self._clock = clock
        self.max_certificates = max_certificates
        #: The security log (A04.FR.04), oldest first.
        self.events: Deque[SecurityEvent] = deque(maxlen=LOG_MAX)
        self._queue: Deque[SecurityEvent] = deque()
        self._wake = asyncio.Event()
        self.roots: Dict[CertUse, List[x509.Certificate]] = {}
        self.client_cert_file, self.client_key_file = client_cert_file, client_key_file
        #: The client certificate before a renewal that has not been confirmed yet.
        self._previous: Optional[Tuple[str, str]] = None
        self._configured = (client_cert_file, client_key_file)
        #: (CSR PEM, its key) of the outstanding CSRs, oldest first.
        self._pending: List[Tuple[str, ec.EllipticCurvePrivateKey]] = []
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        self._serial = 0
        self._client_cache: Optional[Tuple[str, Optional[x509.Certificate]]] = None

    # ------------------------------------------------------------------ events

    def record(self, kind: SecurityEventKind, tech_info: Optional[str] = None, *, connected: bool) -> SecurityEvent:
        """Log ``kind`` now; queue it for the CSMS when it is critical or ``connected``."""
        ev = SecurityEvent(kind, self._clock(), tech_info[:TECH_INFO_MAX] if tech_info else None, kind in CRITICAL)
        self.events.append(ev)
        log.info("security event %s%s", kind.value, f" ({ev.tech_info})" if ev.tech_info else "")
        if ev.critical or connected:
            if len(self._queue) >= QUEUE_MAX:
                dropped = next((e for e in self._queue if not e.critical), self._queue[0])
                self._queue.remove(dropped)
                log.warning("security event queue full: %s dropped", dropped.kind.value)
            self._queue.append(ev)
            self._wake.set()
        return ev

    def connect_failed(self, kind: str, message: str) -> Optional[Tuple[str, str]]:
        """A connection attempt failed (``rpc.transport.HandshakeError.kind``): raise the matching event and, when
        a renewed client certificate has not been confirmed yet and the failure is a TLS / authentication one,
        fall back to the previous certificate (returned as ``(cert, key)`` files)."""
        event = _CONNECT_EVENTS.get(kind)
        if event is not None:
            self.record(event, message, connected=False)
        if kind in _FALLBACK_KINDS and self.renewal_unconfirmed:
            files = self.fall_back()
            self.record(K.DISCARDED_RENEWED_CLIENT_CERTIFICATE, f"connection with the renewed certificate failed: {kind}",
                        connected=False)
            return files
        return None

    @property
    def queued(self) -> Tuple[SecurityEvent, ...]:
        return tuple(self._queue)

    async def deliver(self, send: Callable[[SecurityEvent], Awaitable[Any]]) -> None:
        """Session task: send the queued events in order (see the module docstring); returns when the
        connection is lost."""
        while True:
            while not self._queue:
                self._wake.clear()
                await self._wake.wait()
            ev = self._queue[0]
            try:
                await send(ev)
            except asyncio.CancelledError:
                raise
            except ConnectionError:
                return
            except TimeoutError:
                await asyncio.sleep(RETRY_S)
                continue
            except Exception as e:  # noqa: BLE001 - a CSMS that refuses the event must not block the queue
                log.warning("SecurityEventNotification %s dropped: %r", ev.kind.value, e)
            if self._queue and self._queue[0] is ev:
                self._queue.popleft()

    # ------------------------------------------------------------------ CA certificates (M03 - M05)

    def _all_roots(self) -> List[Tuple[CertUse, x509.Certificate]]:
        return [(use, c) for use, certs in self.roots.items() for c in certs]

    def _issuer_of(self, cert: x509.Certificate) -> Optional[x509.Certificate]:
        for _, c in self._all_roots():
            if c.subject == cert.issuer:
                return c
        return None

    def _hash(self, cert: x509.Certificate, algorithm: str = "SHA256") -> CertificateHashData:
        return hash_data(cert, self._issuer_of(cert), algorithm)

    @property
    def certificate_entries(self) -> int:
        """Installed CA certificates plus the client certificate (2.0.1 CertificateEntries)."""
        return len(self._all_roots()) + (1 if self.client_cert_file else 0)

    def install(self, use: CertUse, pem: str) -> CertStatus:
        try:
            certs = load_chain(pem)
        except ValueError:
            return CertStatus.REJECTED
        if len(certs) != 1 or not _valid_at(certs[0], self._clock()) or not _is_ca(certs[0]):
            return CertStatus.REJECTED
        cert = certs[0]
        stored = self.roots.setdefault(use, [])
        if cert in stored:
            return CertStatus.ACCEPTED
        if len(self._all_roots()) >= self.max_certificates:
            return CertStatus.REJECTED
        stored.append(cert)
        return CertStatus.ACCEPTED

    def installed(self, uses: Sequence[CertUse] = ()) -> List[Tuple[CertUse, CertificateHashData]]:
        return [(use, self._hash(c)) for use, c in self._all_roots() if not uses or use in uses]

    def delete(self, wanted: CertificateHashData) -> CertStatus:
        algorithm = wanted.hash_algorithm
        client = self.client_certificate()
        if client is not None and _same(hash_data(client, self._issuer_of(client), algorithm), wanted):
            return CertStatus.FAILED  # M04.FR.06
        for use, c in self._all_roots():
            if _same(self._hash(c, algorithm), wanted):
                self.roots[use].remove(c)
                return CertStatus.ACCEPTED
        return CertStatus.NOT_FOUND

    def trusted_pem(self) -> Optional[str]:
        """The installed CSMS root certificates as PEM (``None`` when there is none)."""
        certs = self.roots.get(CertUse.CSMS_ROOT) or []
        if not certs:
            return None
        return "".join(c.public_bytes(serialization.Encoding.PEM).decode("ascii") for c in certs)

    # ------------------------------------------------------------------ client certificate (A02 / A03)

    def client_certificate(self) -> Optional[x509.Certificate]:
        path = self.client_cert_file
        if not path:
            return None
        if self._client_cache is None or self._client_cache[0] != path:
            try:
                cert: Optional[x509.Certificate] = load_chain(Path(path).read_text(encoding="ascii"))[0]
            except (OSError, ValueError):
                cert = None
            self._client_cache = (path, cert)
        return self._client_cache[1]

    def new_csr(self, common_name: str, organization: Optional[str]) -> str:
        key, pem = make_csr(common_name, organization)
        self._pending.append((pem, key))
        del self._pending[:-PENDING_MAX]
        return pem

    @property
    def csr_pending(self) -> bool:
        return bool(self._pending)

    def drop_csr(self, csr: str) -> None:
        """The CSMS rejected the SignCertificate of ``csr``: forget its key."""
        self._pending = [p for p in self._pending if p[0] != csr]

    def accept_signed(self, chain_pem: str) -> Tuple[CertStatus, Optional[str]]:
        """Check a CertificateSigned chain; when it is accepted it becomes the client certificate (the caller
        reconnects). Returns the status and, for ``Rejected``, why."""
        try:
            chain = load_chain(chain_pem)
        except ValueError:
            return CertStatus.REJECTED, "certificateChain is not a PEM certificate chain"
        if not self._pending:
            return CertStatus.REJECTED, "no certificate signing request is outstanding"
        leaf = chain[0]
        spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        mine = leaf.public_key().public_bytes(*spki)
        match = next((p for p in self._pending if p[1].public_key().public_bytes(*spki) == mine), None)
        if match is None:
            return CertStatus.REJECTED, "the certificate does not belong to an outstanding CSR"
        if not _valid_at(leaf, self._clock()):
            return CertStatus.REJECTED, "the certificate is not valid now"
        key = match[1]
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="sme-ocpp-sim-")
        self._serial += 1
        directory = Path(self._tmp.name)
        cert_file, key_file = directory / f"client-{self._serial}.pem", directory / f"client-{self._serial}.key"
        pem = serialization.Encoding.PEM
        cert_file.write_bytes(b"".join(c.public_bytes(pem) for c in chain))
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        current = (self.client_cert_file, self.client_key_file) \
            if self.client_cert_file and self.client_key_file else None
        if self._previous is None:
            self._previous = current
        elif current is not None:
            self._discard(current)  # a second renewal before the first was confirmed supersedes the first
        self.client_cert_file, self.client_key_file = str(cert_file), str(key_file)
        self._pending.remove(match)
        return CertStatus.ACCEPTED, None

    def _own(self, path: Optional[str]) -> bool:
        """``path`` lies in this charger's temporary directory."""
        return bool(path) and self._tmp is not None and Path(path).parent == Path(self._tmp.name)

    def _discard(self, pair: Tuple[Optional[str], Optional[str]]) -> None:
        """Delete a superseded / discarded certificate + key pair written by :meth:`accept_signed`."""
        for path in pair:
            if path and self._own(path):
                Path(path).unlink(missing_ok=True)

    @property
    def renewal_unconfirmed(self) -> bool:
        return self._previous is not None

    def confirm(self) -> None:
        """A session with the current client certificate was accepted: forget the previous one (A02.FR.10)."""
        if self._previous is not None:
            self._discard(self._previous)
        self._previous = None

    def fall_back(self) -> Optional[Tuple[str, str]]:
        """Discard an unconfirmed renewed certificate; returns the previous ``(cert, key)`` files, if any."""
        if self._previous is None:
            return None
        self._discard((self.client_cert_file, self.client_key_file))
        self.client_cert_file, self.client_key_file = self._previous
        self._previous = None
        return self.client_cert_file, self.client_key_file

    def close(self) -> Optional[Tuple[str, str]]:
        """The charger stops: delete the temporary directory (renewed certificates and keys) and forget the
        outstanding CSR keys. When the client certificate lived there, the configured one is used again; returns
        it as ``(cert, key)`` files when it changed and one is configured."""
        self._pending.clear()
        if self._tmp is None:
            return None
        reverted = self._own(self.client_cert_file)
        self._tmp.cleanup()
        self._tmp = None
        if not reverted:
            return None
        self._previous = None
        self.client_cert_file, self.client_key_file = self._configured
        if self.client_cert_file and self.client_key_file:
            return self.client_cert_file, self.client_key_file
        return None

    # ------------------------------------------------------------------ view

    def snapshot(self) -> Dict[str, Any]:
        client = self.client_certificate()
        return {
            "client_certificate": subject_text(client) if client is not None else None,
            "client_certificate_expires": client.not_valid_after_utc.isoformat() if client is not None else None,
            "csr_pending": self.csr_pending,
            "certificates": {use.value: len(certs) for use, certs in self.roots.items() if certs},
            "events_queued": len(self._queue),
            "events": [{"type": e.kind.value, "timestamp": e.timestamp.isoformat(), "tech_info": e.tech_info}
                       for e in list(self.events)[-5:]],
        }
