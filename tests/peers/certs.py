"""Throw-away CA, server and client certificates for wss:// / Security Profile 3 tests (uses ``cryptography``)."""

from __future__ import annotations

import datetime as dt
import ipaddress
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PEM = serialization.Encoding.PEM


def _name(cn: str, org: Optional[str] = None) -> x509.Name:
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    if org:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    return x509.Name(attrs)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def make_ca(cn: str = "SME OCPPSim test CA") -> Tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    now = _now()
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(_name(cn)).issuer_name(_name(cn))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return cert, key


@dataclass
class Pki:
    ca_file: Path
    cert_file: Path
    key_file: Path
    ca: x509.Certificate = None  # type: ignore[assignment]
    ca_key: ec.EllipticCurvePrivateKey = None  # type: ignore[assignment]

    def server_context(self, *, client_ca: Optional[Path] = None) -> ssl.SSLContext:
        """TLS server context; ``client_ca`` set = require a client certificate issued by it (profile 3)."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_file, self.key_file)
        if client_ca is not None:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=client_ca)
        return ctx

    @property
    def ca_pem(self) -> str:
        return self.ca.public_bytes(PEM).decode("ascii")

    def sign(self, public_key, subject: x509.Name, *, days: float = 1, not_before: Optional[dt.datetime] = None
             ) -> x509.Certificate:
        now = _now()
        start = not_before or now - dt.timedelta(minutes=5)
        return (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(self.ca.subject)
                .public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(start).not_valid_after(start + dt.timedelta(days=days))
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()),
                               critical=False)
                .sign(self.ca_key, hashes.SHA256()))

    def sign_csr(self, csr_pem: str, **kw) -> str:
        """The PEM chain (leaf + CA) for a CSR, as a CA behind the CSMS would return it."""
        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
        assert csr.is_signature_valid
        leaf = self.sign(csr.public_key(), csr.subject, **kw)
        return (leaf.public_bytes(PEM) + self.ca.public_bytes(PEM)).decode("ascii")

    def client(self, directory: Path, cn: str = "CP1", org: str = "SME") -> Tuple[Path, Path]:
        """A client certificate + key (files) issued by this CA."""
        key = ec.generate_private_key(ec.SECP256R1())
        cert = self.sign(key.public_key(), _name(cn, org))
        cert_file, key_file = directory / f"client-{cn}.pem", directory / f"client-{cn}.key"
        cert_file.write_bytes(cert.public_bytes(PEM))
        key_file.write_bytes(_key_pem(key))
        return cert_file, key_file


def make_pki(directory: Path) -> Pki:
    now = _now()
    ca, ca_key = make_ca()
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(_name("localhost")).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    pki = Pki(directory / "ca.pem", directory / "server.pem", directory / "server.key", ca, ca_key)
    pki.ca_file.write_bytes(ca.public_bytes(PEM))
    pki.cert_file.write_bytes(cert.public_bytes(PEM))
    pki.key_file.write_bytes(_key_pem(key))
    return pki
