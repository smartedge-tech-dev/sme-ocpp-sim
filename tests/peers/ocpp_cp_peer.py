"""Charging stations written with MobilityHouse ``ocpp`` (dev dependency), used ONLY as test peers of the Mini CSMS.

An independent implementation (with its own schema validation of every payload in both directions) next to our
own charger, so a misreading shared by our charger and our Mini CSMS does not pass unnoticed (plan §7 / §11 Q2).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppCP16
from ocpp.v16 import call_result as result16
from ocpp.v201 import ChargePoint as OcppCP201
from ocpp.v201 import call_result as result201
from websockets.asyncio.client import connect


class LibCP16(OcppCP16):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.profiles: list = []
        self.remote_starts: list = []

    @on("SetChargingProfile")
    async def on_set_profile(self, connector_id, cs_charging_profiles, **_):
        self.profiles.append((connector_id, cs_charging_profiles))
        return result16.SetChargingProfile(status="Accepted")

    @on("RemoteStartTransaction")
    async def on_remote_start(self, id_tag, **kw):
        self.remote_starts.append((id_tag, kw))
        return result16.RemoteStartTransaction(status="Accepted")


class LibCP201(OcppCP201):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.profiles: list = []
        self.remote_starts: list = []

    @on("SetChargingProfile")
    async def on_set_profile(self, evse_id, charging_profile, **_):
        self.profiles.append((evse_id, charging_profile))
        return result201.SetChargingProfile(status="Accepted")

    @on("RequestStartTransaction")
    async def on_remote_start(self, id_token, remote_start_id, **kw):
        self.remote_starts.append((id_token, remote_start_id))
        return result201.RequestStartTransaction(status="Accepted")


class LibStation:
    """Connect a library station to ``url/<cp_id>`` and run its receive loop in the background."""

    def __init__(self, cls, cp_id: str, url: str, subprotocol: str) -> None:
        self.cls, self.cp_id, self.url, self.subprotocol = cls, cp_id, url, subprotocol
        self.cp: Optional[OcppCP16] = None
        self._ws = None
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self):
        self._ws = await connect(f"{self.url}/{self.cp_id}", subprotocols=[self.subprotocol])
        self.cp = self.cls(self.cp_id, self._ws, response_timeout=5)
        self._task = asyncio.create_task(self.cp.start())
        return self.cp

    async def __aexit__(self, *exc) -> None:
        await self._ws.close()
        self._task.cancel()
        try:
            await self._task
        except BaseException:  # noqa: BLE001 - ConnectionClosed / CancelledError on teardown
            pass
