"""OCPP 2.1 charging profiles: the 2.0.1 domain types (``protocol.v201.profiles``) extended with the 2.1 fields,
the 2.1 checks of SetChargingProfile and a store that knows about offline validity (2.1-M2).

The 2.0.1 store (validation, replacement, conflicts, clearing) is reused unchanged; the 2.1 rules below run
before it (:func:`reject_reason_21`). Purposes / kinds are the wire enums: the 2.0.1 members plus
``ChargingProfilePurposeEnumTypeExt21`` (PriorityCharging, LocalGeneration) and
``ChargingProfileKindEnumTypeExt21`` (Dynamic).

Rules (OCPP 2.1 Part 2 K01 / K28 / Q, simplest compliant subset)
---------------------------------------------------------------
- Dynamic (K28): exactly one schedule with one period whose startPeriod is 0, else Rejected ``InvalidSchedule``
  (K28.FR.01 - FR.03). ``dynUpdateTime`` is set to the time of receipt when absent (K28.FR.05). The domain copy
  uses ``dynUpdateTime`` as its ``startSchedule``, so the 2.0.1 evaluator runs the period from the last update
  for ``duration`` seconds (K28.FR.13 / FR.15) and falls back to lower profiles afterwards.
- ``dynUpdateInterval`` / ``dynUpdateTime`` on another kind -> Rejected ``InvalidProfile`` (K28.FR.04 /
  K01.FR.122).
- operationMode per purpose (K01.FR.111 - FR.113), only modes of V2XChargingCtrlr.SupportedOperationModes
  (K01.FR.115), bidirectional modes only when V2X is enabled (K01.FR.116: the simulator treats every transaction as
  BPT-capable while V2XChargingCtrlr.Enabled is true) -> else Rejected ``InvalidOperationMode`` (K01.FR.114).
- ``InvalidSchedule``: a ChargingOnly period without ``limit`` (Q "limits and setpoints per operation mode"), an
  Idle period with any limit / dischargeLimit / setpoint (Q10.FR.02), ``evseSleep`` outside Idle (K01.FR.126), a
  positive ``dischargeLimit``, a setpoint outside [dischargeLimit, limit] (V2X.05), ``useLocalTime`` or
  ``randomizedDelay`` > 0 (K01.FR.123 / FR.124: not supported).
- ``PhaseConflict``: dischargeLimit / setpoint _L2 / _L3 fields outside a TxProfile (V2X.09 / V2X.10).
- PriorityCharging / LocalGeneration not in SmartChargingCtrlr.SupportedAdditionalPurposes -> Rejected
  ``UnsupportedPurpose`` (K01.FR.120).
- ``maxOfflineDuration`` (K01.FR.101 - FR.103, Q11 / Q12): while the station has been offline for longer, the
  profile is not valid (lower stack levels take over); when the connection is restored the profiles with
  ``invalidAfterOfflineDuration`` true whose offline time elapsed are removed, the others are valid again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Collection, Optional, Tuple

from ..v201.adapter import charging_schedule as charging_schedule201
from ..v201.profiles import (REASON_INVALID, REASON_UNSUPPORTED_PURPOSE, ChargingProfile, ChargingProfilePurpose,
                             ChargingSchedule, InstalledProfile, ProfileStore, SchedulePeriod)
from . import models as m

__all__ = ["BIDIRECTIONAL_MODES", "DISCHARGE_MODES", "DYNAMIC", "LOCAL_GENERATION", "MODE", "PRIORITY",
           "REASON_INVALID_OPERATION_MODE", "REASON_INVALID_SCHEDULE", "REASON_PHASE_CONFLICT", "ChargingProfile21",
           "ChargingSchedule21", "ProfileStore21", "SchedulePeriod21", "charging_profile21", "mode_of",
           "reject_reason_21"]

MODE = m.OperationModeEnumType
PRIORITY = m.ChargingProfilePurposeEnumTypeExt21.priority_charging
LOCAL_GENERATION = m.ChargingProfilePurposeEnumTypeExt21.local_generation
DYNAMIC = m.ChargingProfileKindEnumTypeExt21.dynamic

REASON_INVALID_SCHEDULE = "InvalidSchedule"
REASON_INVALID_OPERATION_MODE = "InvalidOperationMode"
REASON_PHASE_CONFLICT = "PhaseConflict"

#: Modes that need a bidirectional (BPT) transaction (K01.FR.116).
BIDIRECTIONAL_MODES = frozenset({MODE.central_setpoint, MODE.central_frequency, MODE.local_frequency,
                                 MODE.local_load_balancing, MODE.idle})
#: Modes in which a negative setpoint / a dischargeLimit applies (all but ChargingOnly and Idle).
DISCHARGE_MODES = frozenset(MODE) - {MODE.charging_only, MODE.idle}
#: Allowed operationMode per purpose (K01.FR.111 - FR.113); purposes not listed allow every mode.
_ALLOWED = {
    ChargingProfilePurpose.charging_station_max_profile: {MODE.charging_only},
    PRIORITY: {MODE.charging_only},
    ChargingProfilePurpose.charging_station_external_constraints: {MODE.charging_only, MODE.external_limits,
                                                                    MODE.external_setpoint},
    LOCAL_GENERATION: {MODE.charging_only, MODE.external_limits},
}


@dataclass(frozen=True)
class SchedulePeriod21(SchedulePeriod):
    limit: Optional[float] = None  # optional in 2.1 (a setpoint period may omit it)
    discharge_limit: Optional[float] = None  # <= 0
    setpoint: Optional[float] = None  # positive charges, negative discharges
    operation_mode: Optional[Any] = None  # OperationModeEnumType; None = ChargingOnly (K01.FR.110 note)
    evse_sleep: bool = False
    #: A dischargeLimit / setpoint / setpointReactive _L2 / _L3 field is present (V2X.09 / V2X.10).
    phase_fields: bool = False


@dataclass(frozen=True)
class ChargingSchedule21(ChargingSchedule):
    limit_at_soc: Optional[Tuple[int, float]] = None  # (soc, limit in the schedule unit), K01.FR.100
    use_local_time: bool = False
    randomized_delay: int = 0


@dataclass(frozen=True)
class ChargingProfile21(ChargingProfile):
    max_offline_duration: Optional[int] = None
    invalid_after_offline_duration: bool = False
    dyn_update_interval: Optional[int] = None
    dyn_update_time: Optional[datetime] = None


def mode_of(period: SchedulePeriod) -> Any:
    """The operationMode of a period (2.0.1 periods and an absent field are ChargingOnly)."""
    return getattr(period, "operation_mode", None) or MODE.charging_only


def _period(p: m.ChargingSchedulePeriodType) -> SchedulePeriod21:
    phase_fields = any(x is not None for x in (p.discharge_limit_l2, p.discharge_limit_l3, p.setpoint_l2,
                                                p.setpoint_l3, p.setpoint_reactive_l2, p.setpoint_reactive_l3))
    return SchedulePeriod21(start_period=p.start_period, limit=p.limit, number_phases=p.number_phases,
                            phase_to_use=p.phase_to_use, discharge_limit=p.discharge_limit, setpoint=p.setpoint,
                            operation_mode=p.operation_mode, evse_sleep=bool(p.evse_sleep), phase_fields=phase_fields)


def _schedule(s: m.ChargingScheduleType, start: Optional[datetime]) -> ChargingSchedule21:
    base = charging_schedule201(s)
    cap = (s.limit_at_so_c.soc, s.limit_at_so_c.limit) if s.limit_at_so_c is not None else None
    return ChargingSchedule21(id=base.id, unit=base.unit, periods=tuple(_period(p) for p in s.charging_schedule_period),
                              duration=base.duration, start_schedule=start or base.start_schedule,
                              min_charging_rate=base.min_charging_rate, limit_at_soc=cap,
                              use_local_time=bool(s.use_local_time), randomized_delay=s.randomized_delay or 0)


def charging_profile21(wire: m.ChargingProfileType, now: datetime) -> Tuple[ChargingProfile21, m.ChargingProfileType]:
    """The domain profile of a 2.1 ``ChargingProfileType`` and the wire model to keep (a Dynamic profile gets its
    ``dynUpdateTime``, K28.FR.05)."""
    dyn_time = None
    if wire.charging_profile_kind is DYNAMIC:
        dyn_time = wire.dyn_update_time or now
        wire = wire.model_copy(update={"dyn_update_time": dyn_time})
    first, *others = [_schedule(s, dyn_time) for s in wire.charging_schedule]
    profile = ChargingProfile21(
        profile_id=wire.id, stack_level=wire.stack_level, purpose=wire.charging_profile_purpose,
        kind=wire.charging_profile_kind, schedule=first, transaction_id=wire.transaction_id,
        recurrency_kind=wire.recurrency_kind, valid_from=wire.valid_from, valid_to=wire.valid_to,
        other_schedules=tuple(others), max_offline_duration=wire.max_offline_duration,
        invalid_after_offline_duration=bool(wire.invalid_after_offline_duration),
        dyn_update_interval=wire.dyn_update_interval, dyn_update_time=dyn_time)
    return profile, wire


def _period_reason(purpose: Any, p: SchedulePeriod21, modes: Collection[str], bidirectional: bool) -> Optional[str]:
    mode = mode_of(p)
    allowed = _ALLOWED.get(purpose)
    if (allowed is not None and mode not in allowed) or (mode is not MODE.charging_only and mode.value not in modes):
        return REASON_INVALID_OPERATION_MODE  # K01.FR.111 - FR.115
    if mode in BIDIRECTIONAL_MODES and not bidirectional:
        return REASON_INVALID_OPERATION_MODE  # K01.FR.116
    values = (p.limit, p.discharge_limit, p.setpoint)
    if mode is MODE.idle and any(v is not None for v in values):
        return REASON_INVALID_SCHEDULE  # Q10.FR.02
    if mode is MODE.charging_only and p.limit is None:
        return REASON_INVALID_SCHEDULE  # ChargingOnly: limit required
    if p.evse_sleep and mode is not MODE.idle:
        return REASON_INVALID_SCHEDULE  # K01.FR.126
    if p.discharge_limit is not None and p.discharge_limit > 0:
        return REASON_INVALID_SCHEDULE
    if p.setpoint is not None and ((p.limit is not None and p.setpoint > p.limit)
                                   or (p.discharge_limit is not None and p.setpoint < p.discharge_limit)):
        return REASON_INVALID_SCHEDULE  # V2X.05
    if p.phase_fields and purpose is not ChargingProfilePurpose.tx_profile:
        return REASON_PHASE_CONFLICT  # V2X.09 / V2X.10
    return None


def reject_reason_21(profile: ChargingProfile21, *, modes: Collection[str], purposes: Collection[str],
                     bidirectional: bool) -> Optional[str]:
    """The 2.1 reasonCode why ``profile`` is Rejected before the 2.0.1 checks, ``None`` when they pass.

    ``modes``: V2XChargingCtrlr.SupportedOperationModes; ``purposes``: SmartChargingCtrlr.SupportedAdditionalPurposes;
    ``bidirectional``: the transaction may use bidirectional modes (V2XChargingCtrlr.Enabled)."""
    if profile.purpose in (PRIORITY, LOCAL_GENERATION) and profile.purpose.value not in purposes:
        return REASON_UNSUPPORTED_PURPOSE  # K01.FR.120
    dynamic = profile.kind is DYNAMIC
    if dynamic and (profile.other_schedules or len(profile.schedule.periods) != 1
                    or profile.schedule.periods[0].start_period != 0):
        return REASON_INVALID_SCHEDULE  # K28.FR.01 - FR.03
    if not dynamic and (profile.dyn_update_interval is not None or profile.dyn_update_time is not None):
        return REASON_INVALID  # K28.FR.04 / K01.FR.122
    for sched in (profile.schedule, *profile.other_schedules):
        if getattr(sched, "use_local_time", False) or getattr(sched, "randomized_delay", 0) > 0:
            return REASON_INVALID_SCHEDULE  # K01.FR.123 / FR.124
        for p in sched.periods:
            reason = _period_reason(profile.purpose, p, modes, bidirectional) if isinstance(p, SchedulePeriod21) \
                else None
            if reason is not None:
                return reason
    return None


class ProfileStore21(ProfileStore):
    """The 2.0.1 store plus the offline state of ``maxOfflineDuration`` and dynamic-schedule updates."""

    def __init__(self, evse_count: int) -> None:
        super().__init__(evse_count)
        #: Since when the station is offline (``None`` = online, or never connected yet).
        self.offline_since: Optional[datetime] = None

    def valid_offline(self, profile: ChargingProfile, t: datetime) -> bool:
        """``False`` while the station has been offline for longer than the profile's maxOfflineDuration."""
        limit = getattr(profile, "max_offline_duration", None)
        if limit is None or self.offline_since is None:
            return True  # K01.FR.103
        return t - self.offline_since <= timedelta(seconds=limit)

    def go_offline(self, now: datetime) -> None:
        if self.offline_since is None:
            self.offline_since = now

    def go_online(self, now: datetime) -> Tuple[int, ...]:
        """K01.FR.102: drop the profiles with ``invalidAfterOfflineDuration`` whose maxOfflineDuration elapsed while
        offline; returns their ids."""
        since, self.offline_since = self.offline_since, None
        if since is None:
            return ()
        dead = [ip for ip in self._installed if getattr(ip.profile, "invalid_after_offline_duration", False)
                and now - since > timedelta(seconds=ip.profile.max_offline_duration or 0)
                and ip.profile.max_offline_duration is not None]
        self._installed = [ip for ip in self._installed if ip not in dead]
        return tuple(ip.profile.profile_id for ip in dead)

    def find(self, profile_id: int) -> Optional[InstalledProfile]:
        """The CSMS profile with ``profile_id`` (external constraints excluded)."""
        ext = ChargingProfilePurpose.charging_station_external_constraints
        return next((ip for ip in self._installed if ip.profile.profile_id == profile_id
                     and ip.profile.purpose is not ext), None)

    def update_dynamic(self, profile_id: int, update: m.ChargingScheduleUpdateType, now: datetime) -> InstalledProfile:
        """Apply a ChargingScheduleUpdateType to the Dynamic profile ``profile_id`` (K28.FR.06 / FR.08 / FR.09):
        the values present replace those of its one period, absent ones are kept; ``dynUpdateTime`` = ``now``.
        ``KeyError`` when there is no such profile, ``ValueError`` when it is not Dynamic."""
        ip = self.find(profile_id)
        if ip is None:
            raise KeyError(profile_id)
        if ip.profile.kind is not DYNAMIC:
            raise ValueError(profile_id)
        fields = {k: getattr(update, k) for k in ("limit", "discharge_limit", "setpoint")
                  if getattr(update, k) is not None}
        sched = ip.profile.schedule
        period = replace(sched.periods[0], **fields)
        profile = replace(ip.profile, schedule=replace(sched, periods=(period,), start_schedule=now),
                          dyn_update_time=now)
        wire = ip.wire
        if wire is not None:
            ws = wire.charging_schedule[0]
            wp = ws.charging_schedule_period[0].model_copy(update=update.model_dump(exclude_none=True,
                                                                                    exclude={"custom_data"}))
            wire = wire.model_copy(update={"dyn_update_time": now, "charging_schedule": [
                ws.model_copy(update={"charging_schedule_period": [wp]}), *wire.charging_schedule[1:]]})
        new = InstalledProfile(ip.evse_id, profile, wire)
        self._installed = [new if x is ip else x for x in self._installed]
        return new
