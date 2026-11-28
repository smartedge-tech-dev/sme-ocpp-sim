"""OCPP 2.1 adapter: :class:`~sme_ocpp_sim.protocol.v201.adapter.Adapter201` with the 2.1 models and framing."""

from __future__ import annotations

from typing import Any, Sequence

from ...rpc.errors import OCPP21_ERRORS
from ..v201.adapter import REMOTE_ACTIONS, Adapter201
from . import codec as codec21
from . import models as m

#: CSMS -> CS actions the 2.1 station answers: the 2.0.1 ones plus the store-and-ack 2.1 ones.
REMOTE_ACTIONS_21 = tuple(REMOTE_ACTIONS) + tuple(a for a in codec21.NEW_ACTIONS if a != "ReportDERControl")


class Adapter21(Adapter201):
    version = "2.1"
    subprotocol = "ocpp2.1"
    #: Same codes as 2.0.1; CALLRESULTERROR / SEND framing, unknown MessageTypeId ignored (OCPP-J 2.1).
    error_codes = OCPP21_ERRORS
    models: Any = m
    codec: Any = codec21.CODEC
    remote_actions: Sequence[str] = REMOTE_ACTIONS_21

    async def send_report_der_control(self, report: m.ReportDERControlRequest) -> Any:
        """CS -> CSMS ReportDERControl (R04); returns the parsed response."""
        return await self._send("ReportDERControl", report)
