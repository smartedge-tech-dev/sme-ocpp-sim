"""Minimal OCPP 1.6J charge point built on the self-written RPC layer (spike)."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

from websockets.asyncio.client import connect

from rpc import OCPP16_ERRORS, FrameCallback, RpcConnection

log = logging.getLogger("cp")

SUBPROTOCOL = "ocpp1.6"


@dataclass
class CpStats:
    subprotocol: Optional[str] = None
    boot_status: Optional[str] = None
    heartbeat_interval: Optional[int] = None
    heartbeats_ok: int = 0
    remote_calls: list = field(default_factory=list)  # actions received from CSMS


class ChargePoint16:
    def __init__(
        self,
        csms_url: str,
        cp_id: str,
        *,
        password: Optional[str] = None,
        on_frame: Optional[FrameCallback] = None,
        call_timeout: float = 10.0,
    ) -> None:
        self.url = f"{csms_url.rstrip('/')}/{quote(cp_id, safe='')}"
        self.cp_id = cp_id
        self._password = password
        self._on_frame = on_frame
        self._call_timeout = call_timeout
        self.stats = CpStats()
        self._config = {"HeartbeatInterval": "0"}

    def _headers(self) -> dict:
        if self._password is None:
            return {}
        # Security Profile 1: user = chargePointId. Never log this value.
        token = base64.b64encode(f"{self.cp_id}:{self._password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    # ---------------------------------------------------------- handlers
    async def _on_remote_start(self, payload: dict) -> dict:
        self.stats.remote_calls.append(("RemoteStartTransaction", payload))
        return {"status": "Accepted"}

    async def _on_get_configuration(self, payload: dict) -> dict:
        self.stats.remote_calls.append(("GetConfiguration", payload))
        keys = payload.get("key") or list(self._config)
        known = [{"key": k, "readonly": False, "value": self._config[k]}
                 for k in keys if k in self._config]
        unknown = [k for k in keys if k not in self._config]
        result: dict = {"configurationKey": known}
        if unknown:
            result["unknownKey"] = unknown
        return result

    # ---------------------------------------------------------- main
    async def run(self, stop: asyncio.Event) -> None:
        async with connect(
            self.url,
            subprotocols=[SUBPROTOCOL],
            additional_headers=self._headers(),
            open_timeout=10,
        ) as ws:
            self.stats.subprotocol = ws.subprotocol
            if ws.subprotocol != SUBPROTOCOL:
                # Design §2.4: mismatch is an error (continue-or-not is configurable later).
                raise RuntimeError(f"subprotocol mismatch: {ws.subprotocol!r}")
            rpc = RpcConnection(
                ws,
                {
                    "RemoteStartTransaction": self._on_remote_start,
                    "GetConfiguration": self._on_get_configuration,
                },
                error_codes=OCPP16_ERRORS,
                call_timeout=self._call_timeout,
                on_frame=self._on_frame,
                name=self.cp_id,
            )
            rx = asyncio.create_task(rpc.run())
            try:
                boot = await rpc.call("BootNotification", {
                    "chargePointVendor": "SME", "chargePointModel": "OCPPSim-spike"})
                self.stats.boot_status = boot.get("status")
                interval = int(boot.get("interval") or 60)
                self.stats.heartbeat_interval = interval
                self._config["HeartbeatInterval"] = str(interval)
                if self.stats.boot_status != "Accepted":
                    return  # spike: no Pending/Rejected retry loop
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), interval)
                        break
                    except asyncio.TimeoutError:
                        pass
                    await rpc.call("Heartbeat", {})
                    self.stats.heartbeats_ok += 1
            finally:
                await ws.close()
                await rx
