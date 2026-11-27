"""v0.2 M8: Security Profile 3 (TLS client certificate) and the certificate / security event messages, 1.6 + 2.0.1.

The scenarios run against the ``ocpp``-library CSMS peer of each version over wss:// with a throw-away CA
(``peers.certs``); the peer requires a client certificate issued by that CA (profile 3) unless a test says
otherwise. The version-neutral parts of ``runtime.security`` are tested directly at the end.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from ocpp.v16 import call as call16
from ocpp.v201 import call as call201
from cryptography.hazmat.primitives.serialization import Encoding
from peers.certs import make_ca, make_pki
from pydantic import ValidationError
from test_runtime_charger import CP, FAST, PEERS, _cfg, _scenario, _until

from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.framelog import FrameLog
from sme_ocpp_sim.protocol.base import CertStatus, CertUse, SecurityEventKind
from sme_ocpp_sim.runtime import security as sec
from sme_ocpp_sim.runtime.charger import ChargerRuntime

VERSIONS = pytest.mark.parametrize("version", list(PEERS))


def _v16(version):
    return version == "1.6"


def _name(version, kind: SecurityEventKind) -> str:
    """The wire name of a security event in ``version``."""
    if _v16(version):
        from sme_ocpp_sim.protocol.v16.adapter import SECURITY_EVENT_NAMES
        return SECURITY_EVENT_NAMES.get(kind, kind.value)
    return kind.value


def _events(peer):
    return [t for s in peer.history for t, _ in s.security_events]


def _csrs(peer, version):
    return [c if _v16(version) else c[0] for s in peer.history for c in s.csrs]


def _p3(body, version, tmp_path, *, trust_ca=True, cfg_kw=None):
    """Run ``body(peer, rt, pki)`` with a profile 3 charger against a peer requiring a client certificate."""
    pki = make_pki(tmp_path)
    cert, key = pki.client(tmp_path, cn=CP)

    async def main():
        peer = PEERS[version](hb_interval=30, ssl_context=pki.server_context(client_ca=pki.ca_file))
        await peer.start()
        kw = {"ca_file": str(pki.ca_file) if trust_ca else None, "client_cert_file": str(cert),
              "client_key_file": str(key), "serial": "SN-0001", **(cfg_kw or {})}
        rt = ChargerRuntime(_cfg(**kw), peer.url, version, reconnect=FAST, frame_log=FrameLog(), call_timeout=5)
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            await body(peer, rt, pki)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
            await peer.stop()
    asyncio.run(main())


async def _online(peer, rt, sessions=1):
    await _until(lambda: rt.online and len(peer.history) >= sessions and peer.history[-1].booted.is_set())
    return peer.history[-1]


async def _trigger_csr(peer, version):
    if _v16(version):
        return await peer.request(CP, call16.ExtendedTriggerMessage(requested_message="SignChargePointCertificate"))
    return await peer.request(CP, call201.TriggerMessage(requested_message="SignChargingStationCertificate"))


async def _certificate_signed(peer, version, chain):
    if _v16(version):
        return await peer.request(CP, call16.CertificateSigned(certificate_chain=chain))
    return await peer.request(CP, call201.CertificateSigned(certificate_chain=chain))


async def _install(peer, version, pem, use="CSMS"):
    kind = ("CentralSystemRootCertificate" if use == "CSMS" else "ManufacturerRootCertificate") if _v16(version) \
        else ("CSMSRootCertificate" if use == "CSMS" else "ManufacturerRootCertificate")
    if _v16(version):
        return await peer.request(CP, call16.InstallCertificate(certificate_type=kind, certificate=pem))
    return await peer.request(CP, call201.InstallCertificate(certificate_type=kind, certificate=pem))


async def _installed(peer, version):
    """(status, [hash data dict]) of the CSMS root certificates."""
    if _v16(version):
        r = await peer.request(CP, call16.GetInstalledCertificateIds(certificate_type="CentralSystemRootCertificate"))
        return r.status, list(r.certificate_hash_data or [])
    r = await peer.request(CP, call201.GetInstalledCertificateIds(certificate_type=["CSMSRootCertificate"]))
    return r.status, [c["certificate_hash_data"] for c in r.certificate_hash_data_chain or []]


async def _delete(peer, version, hash_data):
    if _v16(version):
        return await peer.request(CP, call16.DeleteCertificate(certificate_hash_data=hash_data))
    return await peer.request(CP, call201.DeleteCertificate(certificate_hash_data=hash_data))


async def _security_profile(peer, version):
    if _v16(version):
        r = await peer.request(CP, call16.GetConfiguration(key=["SecurityProfile"]))
        return r.configuration_key[0]["value"]
    r = await peer.request(CP, call201.GetVariables(get_variable_data=[
        {"component": {"name": "SecurityCtrlr"}, "variable": {"name": "SecurityProfile"}}]))
    return r.get_variable_result[0]["attribute_value"]


def _serial_of(peercert) -> int:
    return int(peercert["serialNumber"], 16)


# --------------------------------------------------------------------------------------------- profile 3


@VERSIONS
def test_profile_3_connects_with_the_client_certificate_and_no_basic_auth(version, tmp_path):
    """A00.FR.401 - FR.403: the client certificate authenticates the station; no Authorization header."""
    async def body(peer, rt, pki):
        s = await _online(peer, rt)
        assert s.authorization is None and s.peercert is not None
        assert rt.snapshot()["security_profile"] == 3
        assert "CN=CP1" in rt.snapshot()["security"]["client_certificate"]
        assert await _security_profile(peer, version) == "3"
        await _until(lambda: _name(version, SecurityEventKind.STARTUP_OF_THE_DEVICE) in _events(peer))
    _p3(body, version, tmp_path)


@VERSIONS
def test_csr_then_certificate_signed_reconnects_with_the_new_certificate(version, tmp_path):
    """A02: trigger -> SignCertificate(CSR) -> CertificateSigned Accepted -> reconnect with the new certificate."""
    async def body(peer, rt, pki):
        s1 = await _online(peer, rt)
        r = await _trigger_csr(peer, version)
        assert r.status == "Accepted"
        await _until(lambda: _csrs(peer, version))
        csr = x509.load_pem_x509_csr(_csrs(peer, version)[0].encode())
        assert csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "SN-0001"  # A00.FR.511
        assert csr.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value == "SmartEdge"
        assert isinstance(csr.public_key(), ec.EllipticCurvePublicKey)
        if not _v16(version):
            assert s1.csrs[0][1] == "ChargingStationCertificate"
        chain = pki.sign_csr(_csrs(peer, version)[0])
        new_serial = x509.load_pem_x509_certificates(chain.encode())[0].serial_number
        r = await _certificate_signed(peer, version, chain)
        assert r.status == "Accepted"
        s2 = await _online(peer, rt, sessions=2)
        assert _serial_of(s2.peercert) == new_serial != _serial_of(s1.peercert)
        assert rt.transport.client_cert_file != str(tmp_path / f"client-{CP}.pem")
        await _until(lambda: _name(version, SecurityEventKind.RECONFIGURATION_OF_SECURITY_PARAMETERS)
                     in _events(peer))
        await _until(lambda: not rt.station.security.renewal_unconfirmed)  # A02.FR.10: the old one is gone
    _p3(body, version, tmp_path)


@VERSIONS
def test_certificate_signed_rejected_without_csr_or_with_garbage(version, tmp_path):
    """A02.FR.07: Rejected + InvalidChargingStationCertificate; the connection stays."""
    async def body(peer, rt, pki):
        await _online(peer, rt)
        other = ec.generate_private_key(ec.SECP256R1())
        leaf = pki.sign(other.public_key(), x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CP)]))
        assert (await _certificate_signed(peer, version, leaf.public_bytes(Encoding.PEM).decode())).status == "Rejected"
        assert (await _certificate_signed(peer, version, "not a certificate")).status == "Rejected"
        await _trigger_csr(peer, version)
        await _until(lambda: _csrs(peer, version))
        # A certificate for another key than the CSR's is refused too.
        assert (await _certificate_signed(peer, version, leaf.public_bytes(Encoding.PEM).decode())).status == "Rejected"
        name = _name(version, SecurityEventKind.INVALID_CHARGING_STATION_CERTIFICATE)
        await _until(lambda: _events(peer).count(name) == 3)
        assert len(peer.history) == 1
    _p3(body, version, tmp_path)


@VERSIONS
def test_expired_certificate_signed_is_rejected(version, tmp_path):
    async def body(peer, rt, pki):
        await _online(peer, rt)
        await _trigger_csr(peer, version)
        await _until(lambda: _csrs(peer, version))
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
        chain = pki.sign_csr(_csrs(peer, version)[0], not_before=past, days=1)
        assert (await _certificate_signed(peer, version, chain)).status == "Rejected"
    _p3(body, version, tmp_path)


@VERSIONS
def test_renewed_certificate_the_csms_refuses_falls_back_to_the_old_one(version, tmp_path):
    """A02.FR.28 / FR.29: the renewed certificate fails the handshake -> previous one + DiscardedRenewed..."""
    async def body(peer, rt, pki):
        s1 = await _online(peer, rt)
        await _trigger_csr(peer, version)
        await _until(lambda: _csrs(peer, version))
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        chain = make_pki(other_dir).sign_csr(_csrs(peer, version)[0])  # a CA the CSMS does not trust
        assert (await _certificate_signed(peer, version, chain)).status == "Accepted"
        s2 = await _online(peer, rt, sessions=2)
        assert _serial_of(s2.peercert) == _serial_of(s1.peercert)
        name = _name(version, SecurityEventKind.DISCARDED_RENEWED_CLIENT_CERTIFICATE)
        await _until(lambda: name in _events(peer))
        assert rt.transport.client_cert_file == str(tmp_path / f"client-{CP}.pem")
    _p3(body, version, tmp_path)


@VERSIONS
def test_invalid_csms_certificate_event_is_queued_while_offline(version, tmp_path):
    """A00.FR.310 / A04.FR.02: a failed server check raises a critical event, sent once connected."""
    async def body(peer, rt, pki):
        await _until(lambda: rt.last_error is not None and rt.last_error["kind"] == "tls_verify")
        rt.transport = dataclasses.replace(rt.transport, ca_file=str(pki.ca_file))
        await _online(peer, rt)
        await _until(lambda: _name(version, SecurityEventKind.INVALID_CSMS_CERTIFICATE) in _events(peer))
    _p3(body, version, tmp_path, trust_ca=False)


# --------------------------------------------------------------------------------------------- CA certificates


@VERSIONS
def test_install_list_and_delete_ca_certificates(version, tmp_path):
    """M03 / M04 / M05 (1.6 Security: same messages)."""
    async def body(peer, rt, pki):
        await _online(peer, rt)
        assert await _installed(peer, version) == ("NotFound", [])
        assert (await _install(peer, version, pki.ca_pem)).status == "Accepted"
        assert (await _install(peer, version, pki.ca_pem)).status == "Accepted"  # stored once
        assert (await _install(peer, version, "garbage")).status == "Rejected"
        manufacturer, _ = make_ca("Manufacturer root")
        assert (await _install(peer, version, manufacturer.public_bytes(Encoding.PEM).decode(),
                               use="Manufacturer")).status == "Accepted"
        status, hashes = await _installed(peer, version)
        expected = sec.hash_data(pki.ca)
        assert status == "Accepted" and len(hashes) == 1
        assert hashes[0] == {"hash_algorithm": "SHA256", "issuer_name_hash": expected.issuer_name_hash,
                             "issuer_key_hash": expected.issuer_key_hash, "serial_number": expected.serial_number}
        assert rt.transport.ca_data == pki.ca_pem  # trusted for the next TLS connection
        if not _v16(version):
            r = await peer.request(CP, call201.GetVariables(get_variable_data=[
                {"component": {"name": "SecurityCtrlr"}, "variable": {"name": "CertificateEntries"}}]))
            assert r.get_variable_result[0]["attribute_value"] == "3"  # 2 CA certificates + the client one
        # The client certificate cannot be deleted (M04.FR.06).
        client = rt.station.security.client_certificate()
        ch = sec.hash_data(client, pki.ca)
        assert (await _delete(peer, version, {"hash_algorithm": "SHA256", "issuer_name_hash": ch.issuer_name_hash,
                                              "issuer_key_hash": ch.issuer_key_hash,
                                              "serial_number": ch.serial_number})).status == "Failed"
        upper = {**hashes[0], "serial_number": hashes[0]["serial_number"].upper()}
        assert (await _delete(peer, version, upper)).status == "Accepted"
        assert (await _delete(peer, version, hashes[0])).status == "NotFound"
        assert await _installed(peer, version) == ("NotFound", [])
        assert rt.transport.ca_data is None
    _p3(body, version, tmp_path)


def test_201_v2g_certificate_chain_is_not_found(tmp_path):
    async def body(peer, rt, pki):
        await _online(peer, rt)
        await _install(peer, "2.0.1", pki.ca_pem)
        r = await peer.request(CP, call201.GetInstalledCertificateIds(certificate_type=["V2GCertificateChain"]))
        assert r.status == "NotFound"
        r = await peer.request(CP, call201.GetInstalledCertificateIds())
        assert r.status == "Accepted" and r.certificate_hash_data_chain[0]["certificate_type"] == "CSMSRootCertificate"
        r = await peer.request(CP, call201.CertificateSigned(certificate_chain="x", certificate_type="V2GCertificate"))
        assert r.status == "Rejected" and r.status_info["reason_code"] == "NotSupported"
    _p3(body, "2.0.1", tmp_path)


# --------------------------------------------------------------------------------------------- other events


@VERSIONS
def test_reset_raises_reset_or_reboot(version):
    async def body(peer, rt):
        await _until(lambda: rt.online and peer.history and peer.history[-1].booted.is_set())
        # Let the boot-time traffic (statuses, StartupOfTheDevice) settle before the CSMS sends its CALL.
        await _until(lambda: _name(version, SecurityEventKind.STARTUP_OF_THE_DEVICE) in _events(peer))
        if _v16(version):
            await peer.request(CP, call16.Reset(type="Soft"))
        else:
            await peer.request(CP, call201.Reset(type="Immediate"))
        await _until(lambda: len(peer.history) == 2 and rt.online)
        await _until(lambda: _name(version, SecurityEventKind.RESET_OR_REBOOT) in _events(peer))
        startups = _events(peer).count(_name(version, SecurityEventKind.STARTUP_OF_THE_DEVICE))
        assert startups == 1  # the station object survives the reboot
    _scenario(body, version=version)


def test_201_basic_auth_password_change_raises_reconfiguration():
    async def body(peer, rt):
        await _until(lambda: rt.online and peer.history and peer.history[-1].booted.is_set())
        r = await peer.request(CP, call201.SetVariables(set_variable_data=[
            {"component": {"name": "SecurityCtrlr"}, "variable": {"name": "BasicAuthPassword"},
             "attribute_value": "a-new-password-0123"}]))
        assert r.set_variable_result[0]["attribute_status"] == "Accepted"
        await _until(lambda: len(peer.history) == 2 and rt.online)
        name = SecurityEventKind.RECONFIGURATION_OF_SECURITY_PARAMETERS.value
        await _until(lambda: name in _events(peer))
    _scenario(body, version="2.0.1", cfg_kw={"password": "the-old-password-01"})


# --------------------------------------------------------------------------------------------- config


def _sim(profile, **charger):
    url = charger.pop("url", "wss://csms.example.com/ocpp")
    return {"csms_defaults": {"url": url, "security_profile": profile}, "chargers": [{"id": "CP1", **charger}]}


def test_config_profile_3_needs_wss_certificate_and_no_password():
    ok = _sim(3, client_cert_file="c.pem", client_key_file="c.key")
    assert SimConfig.model_validate(ok).csms_defaults.security_profile == 3
    for bad in (_sim(3), _sim(3, client_cert_file="c.pem"),
                _sim(3, client_cert_file="c.pem", client_key_file="c.key", password="pw"),
                _sim(3, client_cert_file="c.pem", client_key_file="c.key", url="ws://csms.example.com/ocpp"),
                _sim(2, client_cert_file="c.pem", client_key_file="c.key", password="pw"),
                _sim(4, client_cert_file="c.pem", client_key_file="c.key")):
        with pytest.raises(ValidationError):
            SimConfig.model_validate(bad)


# --------------------------------------------------------------------------------------------- runtime.security


def _clock():
    return dt.datetime.now(dt.timezone.utc)


def test_critical_events_are_queued_offline_and_non_critical_only_online():
    s = sec.Security(clock=_clock)
    s.record(SecurityEventKind.FAILED_TO_AUTHENTICATE_AT_CSMS, connected=False)
    s.record(SecurityEventKind.INVALID_CSMS_CERTIFICATE, "x" * 300, connected=False)
    s.record(SecurityEventKind.RECONFIGURATION_OF_SECURITY_PARAMETERS, connected=True)
    assert [e.kind for e in s.queued] == [SecurityEventKind.INVALID_CSMS_CERTIFICATE,
                                          SecurityEventKind.RECONFIGURATION_OF_SECURITY_PARAMETERS]
    assert len(s.events) == 3 and len(s.queued[0].tech_info) == sec.TECH_INFO_MAX


def test_delivery_keeps_the_event_when_the_connection_is_lost_and_drops_refused_ones():
    async def main():
        s = sec.Security(clock=_clock)
        s.record(SecurityEventKind.STARTUP_OF_THE_DEVICE, connected=False)
        s.record(SecurityEventKind.RESET_OR_REBOOT, connected=False)

        async def lost(ev):
            raise ConnectionError("gone")
        await s.deliver(lost)  # returns, nothing lost
        assert len(s.queued) == 2
        sent = []

        async def refuse_first(ev):
            if not sent:
                sent.append(None)
                raise RuntimeError("CALLERROR NotImplemented")
            sent.append(ev.kind)
        task = asyncio.create_task(s.deliver(refuse_first))
        await _until(lambda: not s.queued)
        task.cancel()
        assert sent == [None, SecurityEventKind.RESET_OR_REBOOT]
    asyncio.run(main())


def test_hash_data_of_a_self_signed_root(tmp_path):
    import hashlib
    pki = make_pki(tmp_path)
    h = sec.hash_data(pki.ca)
    assert h.issuer_name_hash == hashlib.sha256(pki.ca.issuer.public_bytes()).hexdigest()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    point = pki.ca.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    assert h.issuer_key_hash == hashlib.sha256(point).hexdigest()  # the BIT STRING value of an EC key
    assert h.serial_number == format(pki.ca.serial_number, "x") and not h.serial_number.startswith("0")
    s = sec.Security(clock=_clock)
    assert s.install(CertUse.CSMS_ROOT, pki.ca_pem) is CertStatus.ACCEPTED
    assert s.installed() == [(CertUse.CSMS_ROOT, h)]
    assert s.delete(dataclasses.replace(h, hash_algorithm="SHA384")) is CertStatus.NOT_FOUND  # other digests
    assert s.delete(sec.hash_data(pki.ca, algorithm="SHA384")) is CertStatus.ACCEPTED


def test_install_respects_the_capacity(tmp_path):
    s = sec.Security(clock=_clock, max_certificates=1)
    first, _ = make_ca("A")
    second, _ = make_ca("B")
    assert s.install(CertUse.CSMS_ROOT, first.public_bytes(Encoding.PEM).decode()) is CertStatus.ACCEPTED
    assert s.install(CertUse.MANUFACTURER_ROOT, second.public_bytes(Encoding.PEM).decode()) is CertStatus.REJECTED
    two = (first.public_bytes(Encoding.PEM) + second.public_bytes(Encoding.PEM)).decode()
    assert s.install(CertUse.CSMS_ROOT, two) is CertStatus.REJECTED  # exactly one certificate per request


def test_install_rejects_a_certificate_that_is_no_ca(tmp_path):
    """A non-CA certificate must not become a root (it would be a TLS trust anchor for the CSMS check)."""
    pki = make_pki(tmp_path)
    leaf = pki.sign(ec.generate_private_key(ec.SECP256R1()).public_key(),
                    x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "not a CA")]))
    s = sec.Security(clock=_clock)
    for use in CertUse:
        assert s.install(use, leaf.public_bytes(Encoding.PEM).decode()) is CertStatus.REJECTED
    assert s.install(CertUse.MANUFACTURER_ROOT, pki.ca_pem) is CertStatus.ACCEPTED
    assert s.trusted_pem() is None


def test_full_queue_drops_the_oldest_non_critical_event_first(monkeypatch):
    monkeypatch.setattr(sec, "QUEUE_MAX", 3)
    s = sec.Security(clock=_clock)
    s.record(SecurityEventKind.STARTUP_OF_THE_DEVICE, connected=False)                      # critical
    s.record(SecurityEventKind.RECONFIGURATION_OF_SECURITY_PARAMETERS, connected=True)      # not critical
    s.record(SecurityEventKind.RESET_OR_REBOOT, connected=False)                            # critical
    s.record(SecurityEventKind.FIRMWARE_UPDATED, connected=False)                           # critical
    assert [e.kind for e in s.queued] == [SecurityEventKind.STARTUP_OF_THE_DEVICE, SecurityEventKind.RESET_OR_REBOOT,
                                          SecurityEventKind.FIRMWARE_UPDATED]
    s.record(SecurityEventKind.INVALID_TLS_VERSION, connected=False)  # all critical: the oldest goes
    assert [e.kind for e in s.queued] == [SecurityEventKind.RESET_OR_REBOOT, SecurityEventKind.FIRMWARE_UPDATED,
                                          SecurityEventKind.INVALID_TLS_VERSION]
    assert len(s.events) == 5  # the log keeps every event


def test_delivery_retries_the_head_event_after_a_timeout(monkeypatch):
    monkeypatch.setattr(sec, "RETRY_S", 0.01)

    async def main():
        s = sec.Security(clock=_clock)
        s.record(SecurityEventKind.STARTUP_OF_THE_DEVICE, connected=False)
        s.record(SecurityEventKind.RESET_OR_REBOOT, connected=False)
        sent = []

        async def slow_first(ev):
            sent.append(ev.kind)
            if len(sent) == 1:
                raise TimeoutError
        task = asyncio.create_task(s.deliver(slow_first))
        await _until(lambda: not s.queued)
        task.cancel()
        assert sent == [SecurityEventKind.STARTUP_OF_THE_DEVICE, SecurityEventKind.STARTUP_OF_THE_DEVICE,
                        SecurityEventKind.RESET_OR_REBOOT]
    asyncio.run(main())


def _renewing(tmp_path):
    pki = make_pki(tmp_path)
    cert, key = pki.client(tmp_path)
    s = sec.Security(clock=_clock, client_cert_file=str(cert), client_key_file=str(key))

    def renew():
        assert s.accept_signed(pki.sign_csr(s.new_csr("CP1", None))) == (CertStatus.ACCEPTED, None)
        return s.client_cert_file, s.client_key_file
    return s, (str(cert), str(key)), renew


def test_only_certificate_failures_discard_an_unconfirmed_renewal(tmp_path):
    """A02.FR.28: a network / timeout / CSMS-certificate / other TLS failure keeps the renewed certificate."""
    s, configured, renew = _renewing(tmp_path)
    renewed = renew()
    for kind in ("network", "timeout", "tls", "handshake", "tls_verify", "tls_version", "not_found"):
        assert s.connect_failed(kind, "x") is None, kind
        assert s.renewal_unconfirmed and (s.client_cert_file, s.client_key_file) == renewed
    assert SecurityEventKind.DISCARDED_RENEWED_CLIENT_CERTIFICATE not in [e.kind for e in s.events]
    assert s.connect_failed("client_cert_rejected", "refused") == configured
    assert not s.renewal_unconfirmed
    assert s.events[-1].kind is SecurityEventKind.DISCARDED_RENEWED_CLIENT_CERTIFICATE
    assert not any(Path(p).exists() for p in renewed)  # the discarded pair is deleted
    s.close()


def test_second_renewal_before_confirmation_and_temporary_files(tmp_path):
    import os
    s, configured, renew = _renewing(tmp_path)
    first = renew()
    if os.name != "nt":
        assert os.stat(first[1]).st_mode & 0o777 == 0o600
    second = renew()  # the first renewal was not confirmed yet
    assert s.renewal_unconfirmed and s._previous == configured
    assert not any(Path(p).exists() for p in first) and all(Path(p).exists() for p in second)
    assert s.fall_back() == configured  # back to the original, not to the first renewal
    assert not any(Path(p).exists() for p in second)
    third = renew()
    s.confirm()  # the configured files belong to the user: never deleted
    assert all(Path(p).exists() for p in (*configured, *third))
    tmp_dir = Path(third[0]).parent
    assert s.close() == configured  # the charger stops: the temporary directory goes, the configured pair is back
    assert not tmp_dir.exists() and (s.client_cert_file, s.client_key_file) == configured
    assert s.close() is None


def test_outstanding_csr_keys(tmp_path):
    """A late CertificateSigned for an earlier CSR is still accepted; Rejected drops only that CSR key."""
    pki = make_pki(tmp_path)
    s = sec.Security(clock=_clock)
    first, second = s.new_csr("CP1", None), s.new_csr("CP1", None)
    assert s.accept_signed(pki.sign_csr(first))[0] is CertStatus.ACCEPTED
    s.drop_csr(second)
    assert not s.csr_pending
    assert s.accept_signed(pki.sign_csr(second))[0] is CertStatus.REJECTED
    csrs = [s.new_csr("CP1", None) for _ in range(sec.PENDING_MAX + 1)]
    assert s.accept_signed(pki.sign_csr(csrs[0]))[0] is CertStatus.REJECTED  # beyond PENDING_MAX
    assert s.accept_signed(pki.sign_csr(csrs[-1]))[0] is CertStatus.ACCEPTED
    s.close()


@VERSIONS
def test_sign_certificate_rejected_drops_the_csr_key(version, tmp_path):
    """1.6 ExtendedTriggerMessage / 2.0.1 TriggerMessage -> SignCertificate answered Rejected: the key is dropped."""
    async def body(peer, rt, pki):
        await _online(peer, rt)
        peer.sign_status = "Rejected"
        assert (await _trigger_csr(peer, version)).status == "Accepted"
        await _until(lambda: _csrs(peer, version))
        await _until(lambda: not rt.station.security.csr_pending)
        chain = pki.sign_csr(_csrs(peer, version)[0])
        assert (await _certificate_signed(peer, version, chain)).status == "Rejected"
    _p3(body, version, tmp_path)
