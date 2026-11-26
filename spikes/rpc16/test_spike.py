"""Spike tests: self-written RPC layer + CP against an `ocpp`-library CSMS peer."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from cp import ChargePoint16  # noqa: E402
from csms_peer import CsmsPeer  # noqa: E402


async def _wait_for(cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


def _errors(frames: list) -> dict:
    """uniqueId -> errorCode for every CALLERROR the peer received."""
    out = {}
    for d, raw in frames:
        msg = json.loads(raw)
        if d == "rx" and msg[0] == 4:
            out[msg[1]] = msg[2]
    return out


async def _scenario() -> None:
    peer = CsmsPeer(hb_interval=1, exercise=True)
    port = await peer.start()
    cp_frames: list = []
    cp = ChargePoint16(f"ws://127.0.0.1:{port}", "CP 001/x", password="p:ss:word",
                       on_frame=lambda d, r: cp_frames.append((d, r)))
    stop = asyncio.Event()
    run = asyncio.create_task(cp.run(stop))
    try:
        assert await _wait_for(lambda: cp.stats.heartbeats_ok >= 2, 8), cp.stats
        s = peer.sessions["CP 001/x"]  # id was URL-encoded on the wire
        assert await asyncio.wait_for(s.exercised.wait(), 5)
        assert await _wait_for(lambda: len(_errors(s.frames)) >= 4, 3), s.frames

        # subprotocol negotiated on both ends
        assert cp.stats.subprotocol == "ocpp1.6" and s.subprotocol == "ocpp1.6"
        # Basic Auth, RFC 7617: split on the FIRST ':' only
        scheme, token = s.authorization.split(" ", 1)
        user, pw = base64.b64decode(token).decode().split(":", 1)
        assert (scheme, user, pw) == ("Basic", "CP 001/x", "p:ss:word")
        # Boot / Heartbeat
        assert cp.stats.boot_status == "Accepted" and cp.stats.heartbeat_interval == 1
        assert s.boots == 1 and s.heartbeats >= 2
        # remote CALLs got correct CALLRESULTs (validated by the ocpp lib's schemas)
        assert s.results["RemoteStartTransaction"] == "Accepted"
        conf, unknown = s.results["GetConfiguration"]
        assert conf == [{"key": "HeartbeatInterval", "readonly": False, "value": "1"}]
        assert unknown == ["NoSuchKey"]
        # CALLERROR paths
        assert s.results["FooBarUnknownAction"] == "NotImplemented"
        errs = _errors(s.frames)
        assert errs["bad-payload"] == "FormationViolation"
        assert errs["bad-shape"] == "FormationViolation"
        assert errs["bad-type"] == "ProtocolError"
        # CP-side frame log saw both directions
        assert {d for d, _ in cp_frames} == {"rx", "tx"}
    finally:
        stop.set()
        await asyncio.wait_for(run, 5)
        await peer.stop()


def test_cp_against_ocpp_peer() -> None:
    t0 = time.monotonic()
    asyncio.run(_scenario())
    assert time.monotonic() - t0 < 30


async def _probe_no_subprotocol() -> str:
    """Record (not assert) what websockets' server does when the client offers
    a subprotocol the server does not support."""
    from websockets.asyncio.client import connect
    peer = CsmsPeer(exercise=False)
    port = await peer.start()
    try:
        async with connect(f"ws://127.0.0.1:{port}/X", subprotocols=["ocpp2.0.1"]) as ws:
            return f"connected, subprotocol={ws.subprotocol!r}"
    except Exception as e:  # noqa: BLE001
        return f"rejected: {type(e).__name__}: {e}"
    finally:
        await peer.stop()


def test_probe_subprotocol_mismatch() -> None:
    print("\nPROBE subprotocol mismatch ->", asyncio.run(_probe_no_subprotocol()))
