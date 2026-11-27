"""Transport: URL, Basic Auth, TLS (profile 2), handshake error classes, back-off, password hygiene."""

import asyncio
import base64
import logging
import socket
from http import HTTPStatus

import pytest
from peers.certs import make_pki
from websockets.asyncio.server import serve

from sme_ocpp_sim.rpc import Backoff, HandshakeError, TransportConfig, open_connection, wait_or_stop
from sme_ocpp_sim.rpc.transport import basic_auth_header

SECRET = "s3cr3t:pw"


# ---------------------------------------------------------------------- config / header

def test_url_encodes_charger_id_and_profiles():
    cfg = TransportConfig("ws://h:9/ocpp/", "CP 001/x")
    assert cfg.url == "ws://h:9/ocpp/CP%20001%2Fx"
    assert cfg.security_profile == 0
    assert TransportConfig("ws://h/o", "A", password="p").security_profile == 1
    assert TransportConfig("wss://h/o", "A", password="p").security_profile == 2
    assert TransportConfig("wss://h/o", "A").security_profile is None


def test_basic_auth_splits_on_first_colon_only():
    scheme, token = basic_auth_header("CP1", SECRET).split(" ", 1)
    assert scheme == "Basic"
    assert base64.b64decode(token).decode().split(":", 1) == ["CP1", SECRET]
    with pytest.raises(ValueError):
        basic_auth_header("CP:1", "pw")


def test_password_not_in_repr():
    assert SECRET not in repr(TransportConfig("ws://h/o", "CP1", password=SECRET))


# ---------------------------------------------------------------------- back-off

def test_backoff_sequence_cap_and_reset():
    b = Backoff(initial=1, factor=2, maximum=10, jitter=0.5, rng=lambda: 0.0)
    assert [b.next() for _ in range(6)] == [1, 2, 4, 8, 10, 10]
    b.reset()
    assert b.next() == 1
    half = Backoff(initial=4, maximum=4, jitter=0.5, rng=lambda: 1.0)
    assert half.next() == 2.0
    with pytest.raises(ValueError):
        Backoff(initial=0)
    with pytest.raises(ValueError):
        Backoff(jitter=1.5)


def test_wait_or_stop():
    async def main():
        stop = asyncio.Event()
        assert await wait_or_stop(stop, 0.01) is False
        asyncio.get_running_loop().call_later(0.01, stop.set)
        assert await wait_or_stop(stop, 5) is True
    asyncio.run(main())


# ---------------------------------------------------------------------- handshake against a real server

async def _echo(ws):
    async for _ in ws:
        pass


def _status_server(status):
    def process_request(connection, request):
        return connection.respond(status, "no\n")
    return process_request


async def _try(cfg_kw, **server_kw):
    async with serve(_echo, "127.0.0.1", 0, **server_kw) as server:
        port = server.sockets[0].getsockname()[1]
        scheme = "wss" if server_kw.get("ssl") else "ws"
        cfg = TransportConfig(f"{scheme}://127.0.0.1:{port}/ocpp", "CP1", **cfg_kw)
        ws = await open_connection(cfg)
        sub = ws.subprotocol
        await ws.close()
        return sub


@pytest.mark.parametrize("status, kind", [
    (HTTPStatus.BAD_REQUEST, "bad_request"), (HTTPStatus.UNAUTHORIZED, "unauthorized"),
    (HTTPStatus.FORBIDDEN, "forbidden"), (HTTPStatus.NOT_FOUND, "not_found"),
    (HTTPStatus.INTERNAL_SERVER_ERROR, "http_error"),
])
def test_handshake_status_is_classified(status, kind):
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(_try({}, subprotocols=["ocpp1.6"], process_request=_status_server(status)))
    assert (ei.value.kind, ei.value.status) == (kind, int(status))
    if kind == "not_found":
        assert "not registered" in str(ei.value) and "subprotocol" in str(ei.value)


def test_subprotocol_mismatch_rejected_by_websockets_server_is_bad_request():
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(_try({}, subprotocols=["ocpp2.0.1"]))
    assert ei.value.kind == "bad_request"


def test_missing_subprotocol_echo_is_an_error_unless_relaxed():
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(_try({}))                               # server offers no subprotocols
    assert ei.value.kind == "subprotocol"
    assert asyncio.run(_try({"require_subprotocol": False})) is None


def test_network_and_url_errors():
    with socket.socket() as s:                              # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(open_connection(TransportConfig(f"ws://127.0.0.1:{port}/o", "CP1", open_timeout=3)))
    assert ei.value.kind in ("network", "timeout")
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(open_connection(TransportConfig("http://127.0.0.1/o", "CP1")))
    assert ei.value.kind == "bad_url"


def test_password_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(_try({"password": SECRET}, subprotocols=["ocpp1.6"],
                         process_request=_status_server(HTTPStatus.UNAUTHORIZED)))
    token = basic_auth_header("CP1", SECRET).split(" ", 1)[1]
    client = [r.getMessage() for r in caplog.records if not r.name.startswith("websockets.server")]
    assert any("Authorization: Basic [redacted]" in m for m in client)   # header was logged, redacted
    for text in (*client, str(ei.value)):
        assert SECRET not in text and token not in text


# ---------------------------------------------------------------------- profile 2 (wss + own CA)

def test_wss_with_own_ca(tmp_path):
    pki = make_pki(tmp_path)
    server = {"subprotocols": ["ocpp1.6"], "ssl": pki.server_context()}
    assert asyncio.run(_try({"password": "pw", "ca_file": str(pki.ca_file)}, **server)) == "ocpp1.6"
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(_try({"password": "pw"}, **server))     # system CAs do not know ours
    assert ei.value.kind == "tls_verify"
    assert asyncio.run(_try({"password": "pw", "verify_tls": False}, **server)) == "ocpp1.6"


# ---------------------------------------------------------------------- profile 3 (wss + client certificate)

def test_profile_3_property():
    cfg = TransportConfig("wss://h/o", "A", client_cert_file="c.pem", client_key_file="c.key")
    assert cfg.security_profile == 3
    assert TransportConfig("wss://h/o", "A", password="p", client_cert_file="c.pem").security_profile is None
    assert TransportConfig("ws://h/o", "A", client_cert_file="c.pem").security_profile is None


def _p3(tmp_path):
    pki = make_pki(tmp_path)
    return pki, {"subprotocols": ["ocpp1.6"], "ssl": pki.server_context(client_ca=pki.ca_file)}


def test_profile_3_correct_client_certificate(tmp_path):
    pki, server = _p3(tmp_path)
    cert, key = pki.client(tmp_path)
    kw = {"ca_file": str(pki.ca_file), "client_cert_file": str(cert), "client_key_file": str(key)}
    assert asyncio.run(_try(kw, **server)) == "ocpp1.6"


def test_profile_3_no_client_certificate_is_rejected(tmp_path):
    pki, server = _p3(tmp_path)
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(_try({"ca_file": str(pki.ca_file), "open_timeout": 5}, **server))
    # Without a configured client certificate the charger did not try profile 3: a plain TLS / handshake error.
    assert ei.value.kind in ("tls", "handshake", "network")


def test_profile_3_client_certificate_of_another_ca_is_rejected(tmp_path):
    pki, server = _p3(tmp_path)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    cert, key = make_pki(other_dir).client(other_dir)
    kw = {"ca_file": str(pki.ca_file), "client_cert_file": str(cert), "client_key_file": str(key), "open_timeout": 5}
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(_try(kw, **server))
    assert ei.value.kind == "client_cert_rejected"
    assert "client certificate" in str(ei.value)


def test_profile_3_unloadable_client_certificate(tmp_path):
    pki, server = _p3(tmp_path)
    kw = {"ca_file": str(pki.ca_file), "client_cert_file": str(tmp_path / "missing.pem"),
          "client_key_file": str(tmp_path / "missing.key")}
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(_try(kw, **server))
    assert ei.value.kind == "client_cert"


def test_installed_root_is_trusted_via_ca_data(tmp_path):
    pki, server = _p3(tmp_path)
    cert, key = pki.client(tmp_path)
    kw = {"ca_data": pki.ca_pem, "client_cert_file": str(cert), "client_key_file": str(key)}
    assert asyncio.run(_try(kw, **server)) == "ocpp1.6"


def test_unloadable_ca_file_is_a_handshake_error(tmp_path):
    """A missing ca_file must not escape as OSError (the charger retries with back-off instead of dying)."""
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(open_connection(TransportConfig("wss://127.0.0.1:1/o", "CP1", ca_file=str(tmp_path / "no.pem"))))
    assert ei.value.kind == "tls"
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(open_connection(TransportConfig("wss://127.0.0.1:1/o", "CP1", ca_data="not PEM")))
    assert ei.value.kind == "tls"


def test_only_certificate_alerts_mean_client_cert_rejected():
    import ssl
    from sme_ocpp_sim.rpc.transport import classify_ssl
    cfg = TransportConfig("wss://h/o", "A", client_cert_file="c.pem", client_key_file="c.key")

    def err(reason):
        e = ssl.SSLError(1, reason)
        e.reason = reason
        return e
    assert classify_ssl(err("TLSV1_ALERT_UNKNOWN_CA"), cfg).kind == "client_cert_rejected"
    assert classify_ssl(err("TLSV13_ALERT_CERTIFICATE_REQUIRED"), cfg).kind == "client_cert_rejected"
    assert classify_ssl(err("TLSV1_ALERT_INTERNAL_ERROR"), cfg).kind == "tls"
    assert classify_ssl(err("SSLV3_ALERT_HANDSHAKE_FAILURE"), cfg).kind == "tls"
