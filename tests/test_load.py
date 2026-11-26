"""Load test (plan §6.4): N chargers against the in-process dev peer.

Excluded by default; run with ``pytest -m load -s``. Environment:

* ``SME_LOAD_CHARGERS`` (200), ``SME_LOAD_SECONDS`` (600)
* ``SME_LOAD_HEARTBEAT_S`` (10) - interval the peer answers in BootNotification
* ``SME_LOAD_METER_S`` (60) - MeterValueSampleInterval; every charger runs one transaction
"""

import asyncio
import os
import time

import pytest
from peers.ocpp16_peer import CsmsPeer

from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine

N = int(os.environ.get("SME_LOAD_CHARGERS", "200"))
SECONDS = float(os.environ.get("SME_LOAD_SECONDS", "600"))
HEARTBEAT = int(os.environ.get("SME_LOAD_HEARTBEAT_S", "10"))
METER = int(os.environ.get("SME_LOAD_METER_S", "60"))


@pytest.mark.load
def test_many_chargers_stay_connected():
    async def main():
        peer = CsmsPeer(hb_interval=HEARTBEAT)
        await peer.start()
        cfg = SimConfig.model_validate({
            "csms_defaults": {"url": peer.url},
            "chargers": [{"id": f"CP-{i:04d}", "auth_mode": "accept_all",
                          "config_keys": {"MeterValueSampleInterval": METER}} for i in range(N)],
        })
        engine = OcppEngine(cfg)
        q = engine.subscribe()  # one UI-like subscriber: exercises the throttled push
        await engine.start()
        try:
            async with asyncio.timeout(120):
                while sum(rt.online for rt in engine.chargers.values()) < N:
                    await asyncio.sleep(0.2)
            for rt in engine.chargers.values():
                await rt.plug(1)
                await rt.swipe(1, "LOAD")

            loop = asyncio.get_running_loop()
            lag, pushes = 0.0, 0
            cpu0, wall0 = time.process_time(), time.monotonic()
            while time.monotonic() - wall0 < SECONDS:
                t = loop.time()
                await asyncio.sleep(0.1)
                lag = max(lag, loop.time() - t - 0.1)
                while not q.empty():
                    q.get_nowait()
                    pushes += 1
            cpu = (time.process_time() - cpu0) / (time.monotonic() - wall0)

            rts = engine.chargers.values()
            timeouts = sum(1 for f in engine.frame_log.since(0) if "timeout" in f.note.lower())
            reconnects = sum(rt.connect_attempts - 1 for rt in rts)
            charging = sum(1 for rt in rts if rt.conns[1].tx is not None)
            print(f"\nload: {N} chargers x {SECONDS:g}s, heartbeat {HEARTBEAT}s, meter {METER}s: "
                  f"online {sum(rt.online for rt in rts)}/{N}, charging {charging}, reconnects {reconnects}, "
                  f"timeout notes {timeouts}, CPU {cpu:.0%} of one core, max loop lag {lag * 1000:.0f} ms, "
                  f"pushes {pushes}, frames {engine.frame_log.last_seq}")
            assert all(rt.online for rt in rts) and reconnects == 0 and timeouts == 0
            assert lag < engine.tick_seconds
        finally:
            await engine.stop()
            await peer.stop()
    asyncio.run(main())
