"""OCPP 2.1 adapter: :class:`~sme_ocpp_sim.protocol.v201.adapter.Adapter201` with the 2.1 models and framing."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Sequence, Tuple

from ...rpc.errors import OCPP21_ERRORS
from .. import base as d
from ..v201.adapter import REMOTE_ACTIONS, Adapter201, id_token, id_token_info
from . import codec as codec21
from . import models as m
from .tariff import tariff_key, with_utc

#: CSMS -> CS actions the 2.1 station answers: the 2.0.1 ones plus the store-and-ack 2.1 ones.
REMOTE_ACTIONS_21 = tuple(REMOTE_ACTIONS) + tuple(a for a in codec21.NEW_ACTIONS if a not in codec21.CS_ACTIONS)


class Adapter21(Adapter201):
    version = "2.1"
    subprotocol = "ocpp2.1"
    #: Same codes as 2.0.1; CALLRESULTERROR / SEND framing, unknown MessageTypeId ignored (OCPP-J 2.1).
    error_codes = OCPP21_ERRORS
    models: Any = m
    codec: Any = codec21.CODEC
    remote_actions: Sequence[str] = REMOTE_ACTIONS_21
    #: Driver-specific tariffs kept per idToken from an Accepted AuthorizeResponse (I08); bounded, oldest first.
    MAX_DRIVER_TARIFFS = 64

    def __init__(self, *, strict: bool = False) -> None:
        super().__init__(strict=strict)
        self.driver_tariffs: "OrderedDict[Tuple[str, str], m.TariffType]" = OrderedDict()

    async def send_authorize(self, ev: d.AuthorizeRequest) -> d.IdTokenInfo:
        """As 2.0.1; the ``tariff`` of an Accepted answer is kept for the idToken (I08.FR.04), see the station."""
        token = id_token(ev.id_token, models=self.models)
        res: m.AuthorizeResponse = await self._send("Authorize", self.models.AuthorizeRequest(id_token=token))
        key = tariff_key(ev.id_token, token.type)
        self.driver_tariffs.pop(key, None)  # every answer replaces the cached tariff, also one without a tariff
        if res.tariff is not None and res.id_token_info.status.value == "Accepted":
            self.driver_tariffs[key] = with_utc(res.tariff)
            while len(self.driver_tariffs) > self.MAX_DRIVER_TARIFFS:
                self.driver_tariffs.popitem(last=False)
        return id_token_info(res.id_token_info)

    async def send_notify_event(self, req: m.NotifyEventRequest) -> Any:
        """CS -> CSMS NotifyEvent (G01 connector AvailabilityState)."""
        return await self._send("NotifyEvent", req)

    async def send_report_der_control(self, report: m.ReportDERControlRequest) -> Any:
        """CS -> CSMS ReportDERControl (R04); returns the parsed response."""
        return await self._send("ReportDERControl", report)

    async def send_pull_dynamic_schedule_update(self, profile_id: int) -> m.PullDynamicScheduleUpdateResponse:
        """CS -> CSMS PullDynamicScheduleUpdate (K28 scenario #2); returns the parsed response."""
        return await self._send("PullDynamicScheduleUpdate",
                                self.models.PullDynamicScheduleUpdateRequest(charging_profile_id=profile_id))

    async def send_notify_priority_charging(self, transaction_id: str, activated: bool) -> Any:
        """CS -> CSMS NotifyPriorityCharging (K21.FR.05 / FR.08, K22)."""
        return await self._send("NotifyPriorityCharging", self.models.NotifyPriorityChargingRequest(
            transaction_id=transaction_id, activated=activated))
