"""The 2.0.1 codec (``protocol.v201.codec``) bound to the OCPP 2.1 models."""

from __future__ import annotations

from ..v201 import models as m201
from ..v201.codec import Codec, dump
from . import models as m

#: 2.1 actions with a model in the tables: every 2.0.1 one plus the new 2.1 ones the station answers or sends.
NEW_ACTIONS = (
    # CSMS -> CS, store-and-ack (tier (c) of the 2.1 delta)
    "AFRRSignal", "NotifyAllowedEnergyTransfer",
    "SetDefaultTariff", "GetTariffs", "ClearTariffs", "ChangeTransactionTariff",
    "SetDERControl", "GetDERControl", "ClearDERControl",
    # CSMS -> CS, smart charging / V2X (2.1-M2)
    "UpdateDynamicSchedule", "UsePriorityCharging",
    # CS -> CSMS: the answer to GetDERControl, dynamic-schedule pulls and priority-charging notices
    "ReportDERControl", "PullDynamicScheduleUpdate", "NotifyPriorityCharging",
)
#: CS -> CSMS actions among :data:`NEW_ACTIONS` (not answered by the station).
CS_ACTIONS = ("ReportDERControl", "PullDynamicScheduleUpdate", "NotifyPriorityCharging")
ACTIONS = tuple(m201.ACTIONS) + NEW_ACTIONS

REQUESTS = {a: getattr(m, f"{a}Request") for a in ACTIONS}
RESPONSES = {a: getattr(m, f"{a}Response") for a in ACTIONS}

CODEC = Codec(REQUESTS, RESPONSES)
model_for = CODEC.model_for
parse = CODEC.parse

__all__ = ["ACTIONS", "CODEC", "CS_ACTIONS", "NEW_ACTIONS", "REQUESTS", "RESPONSES", "dump", "model_for", "parse"]
