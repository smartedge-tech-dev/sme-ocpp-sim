"""Test PKI for the Security Profile 3 (mTLS) interop checks; see scripts/citrineos.md and scripts/steve.md.

Usage:
    .venv/Scripts/python scripts/mtls_certs.py <out-dir> [--client SME-SIM-203 --client SME-SIM-3 ...]

Writes (all PEM, keys unencrypted; <out-dir> is a throwaway directory outside the repo - never commit it):
  root.pem / root.key            trusted root CA (the CSMS trusts client certificates chaining to it)
  server.pem / server.key        CSMS server certificate, SAN localhost / 127.0.0.1 / host.docker.internal
  subca.pem / subca.key          charger sub-CA, signed by the root
  client-<id>.pem / .key         charger certificate (CN = <id>, chain client + sub-CA), one per --client
  untrusted-<id>.pem / .key      the same CN from a self-made root the CSMS does not trust
  citrineos/                     the four files CitrineOS' SP3 server entry reads (leafKey, certChain,
                                 rootCertificate, subCAKey), mounted by scripts/citrineos-mtls.compose.yml
  nginx/                         server.pem / server.key / client-ca.pem for scripts/steve-mtls-nginx.conf

The module also holds the shared SP3 check (:func:`sp3_rejections`) used by the e2e scripts.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import ipaddress
from pathlib import Path
from typing import Callable, Dict, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_CLIENTS = ["SME-SIM-203", "SME-SIM-3"]


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SME OCPPSim test"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _cert(subject: str, key, issuer: Optional[x509.Certificate], issuer_key, *, ca: bool,
          server: bool = False, client: bool = False) -> x509.Certificate:
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(_name(subject))
         .issuer_name(issuer.subject if issuer else _name(subject))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(days=365))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if issuer is not None:
        b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                            critical=False)
    if ca:
        b = b.add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
    else:
        b = b.add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
    if server:
        b = b.add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.DNSName("host.docker.internal"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        b = b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    if client:
        b = b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


def _pem(*certs: x509.Certificate) -> bytes:
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def generate(out: Path, clients: List[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    files: Dict[str, bytes] = {}
    root_key = _key()
    root = _cert("OCPPSim test root", root_key, None, root_key, ca=True)
    srv_key = _key()
    srv = _cert("localhost", srv_key, root, root_key, ca=False, server=True)
    sub_key = _key()
    sub = _cert("OCPPSim test charger subCA", sub_key, root, root_key, ca=True)
    bad_root_key = _key()
    bad_root = _cert("OCPPSim untrusted root", bad_root_key, None, bad_root_key, ca=True)
    files.update({"root.pem": _pem(root), "root.key": _key_pem(root_key),
                  "server.pem": _pem(srv, root), "server.key": _key_pem(srv_key),
                  "subca.pem": _pem(sub), "subca.key": _key_pem(sub_key)})
    for cid in clients:
        k = _key()
        files[f"client-{cid}.pem"] = _pem(_cert(cid, k, sub, sub_key, ca=False, client=True), sub)
        files[f"client-{cid}.key"] = _key_pem(k)
        k = _key()
        files[f"untrusted-{cid}.pem"] = _pem(_cert(cid, k, bad_root, bad_root_key, ca=False, client=True))
        files[f"untrusted-{cid}.key"] = _key_pem(k)
    files.update({"citrineos/leafKey.pem": _key_pem(srv_key), "citrineos/certChain.pem": _pem(srv, root),
                  "citrineos/rootCertificate.pem": _pem(root), "citrineos/subCAKey.pem": _key_pem(sub_key),
                  "nginx/server.pem": _pem(srv, root), "nginx/server.key": _key_pem(srv_key),
                  "nginx/client-ca.pem": _pem(root)})
    for rel, data in files.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def tls_fields(certs: Path, cp_id: str, kind: str) -> dict:
    """ChargerConfig fields for ``kind`` = "good" (trusted client cert), "untrusted" or "none" (no client cert;
    the CSMS certificate is still verified against root.pem)."""
    fields: dict = {"ca_file": str(certs / "root.pem")}
    if kind != "none":
        prefix = "client" if kind == "good" else "untrusted"
        fields.update(client_cert_file=str(certs / f"{prefix}-{cp_id}.pem"),
                      client_key_file=str(certs / f"{prefix}-{cp_id}.key"))
    return fields


async def sp3_rejections(make_runtime: Callable[[str], object], timeout: float = 15.0) -> dict:
    """Run a runtime without a client certificate and one with an untrusted one; each must never come online.
    ``make_runtime(kind)`` returns a ChargerRuntime (with a short reconnect backoff)."""
    out: dict = {}
    for kind in ("none", "untrusted"):
        rt = make_runtime(kind)
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            loop = asyncio.get_running_loop()
            end = loop.time() + timeout
            while loop.time() < end and not rt.online and not (rt.connect_attempts >= 2 and rt.last_error):
                await asyncio.sleep(0.2)
            out[kind] = {"rejected": not rt.online and rt.last_error is not None, "online": rt.online,
                         "last_error": rt.last_error, "attempts": rt.connect_attempts}
        finally:
            stop.set()
            await asyncio.wait_for(task, 10)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--client", action="append", help=f"charger id (default: {' '.join(DEFAULT_CLIENTS)})")
    a = ap.parse_args()
    generate(Path(a.out), a.client or DEFAULT_CLIENTS)
    print(f"written to {Path(a.out).resolve()}")
