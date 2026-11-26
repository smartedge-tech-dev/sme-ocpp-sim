"""Load spike: N charge points against csms_peer in ONE process (both sides).

Usage: python load_spike.py [N] [seconds] [heartbeat_interval]
CPU time reported covers CP side + ocpp-lib CSMS side together.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from cp import ChargePoint16  # noqa: E402
from csms_peer import CsmsPeer  # noqa: E402


async def main(n: int, seconds: float, hb: int) -> None:
    peer = CsmsPeer(hb_interval=hb, exercise=False)
    port = await peer.start()
    stop = asyncio.Event()
    cps = [ChargePoint16(f"ws://127.0.0.1:{port}", f"CP{i:04d}") for i in range(n)]
    cpu0, wall0 = time.process_time(), time.monotonic()
    tasks = [asyncio.create_task(c.run(stop)) for c in cps]
    await asyncio.sleep(seconds)
    stop.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    cpu, wall = time.process_time() - cpu0, time.monotonic() - wall0
    await peer.stop()
    failures = [r for r in results if isinstance(r, BaseException)]
    booted = sum(c.stats.boot_status == "Accepted" for c in cps)
    hbs = sum(c.stats.heartbeats_ok for c in cps)
    print(f"N={n} run={seconds}s hb={hb}s  connected/booted={booted}/{n}  "
          f"failures={len(failures)}  heartbeats={hbs} ({hbs / seconds:.0f}/s)  "
          f"peer_sessions={len(peer.sessions)}  wall={wall:.2f}s  cpu={cpu:.2f}s "
          f"({100 * cpu / wall:.0f}% of one core)")
    for f in failures[:3]:
        print("  failure:", type(f).__name__, f)


if __name__ == "__main__":
    a = sys.argv[1:]
    asyncio.run(main(int(a[0]) if a else 200, float(a[1]) if len(a) > 1 else 10,
                     int(a[2]) if len(a) > 2 else 1))
