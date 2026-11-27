"""OCPP 2.0.1 charging profile domain types and the per-station :class:`ProfileStore` (plan §11.2, M5).

A copy of ``protocol/v16/profiles.py`` adapted to 2.0.1; the two versions share no smart-charging code. The
wire enums of this package (``ChargingProfilePurposeEnumType`` ...) are the domain enums here.

Rules implemented from the OCPP 2.0.1 JSON schemas: purposes ChargingStationMaxProfile / TxDefaultProfile /
TxProfile / ChargingStationExternalConstraints, ``evseId`` 0 = the whole station, a ``chargingSchedule`` array of
1..3 schedules each with its own ``id``, string ``transactionId`` (maxLength 36), ``phaseToUse``.

Interpretations (not defined by the schemas; the 2.0.1 spec text was not available locally, so the rules
recalled from it are marked UNVERIFIED)
--------------------------------------------------------------------------------------------------------
- ChargingStationMaxProfile only on evseId 0; TxProfile only on evseId > 0 while a transaction runs there
  (UNVERIFIED, as 1.6 §3.13.1). A TxProfile of SetChargingProfile must carry the ``transactionId`` of that
  transaction (UNVERIFIED, recalled K01.FR.03 / FR.09); the profile of RequestStartTransaction has none and is
  bound to the transaction it starts (:meth:`ProfileStore.validate` with ``for_remote_start``).
- ChargingStationExternalConstraints cannot be set by the CSMS (Rejected, UNVERIFIED); only the operator's
  external limit (``external=True``) installs one, and ClearChargingProfile never removes one.
- Replacement: a profile replaces an installed one with the same ``id`` (on any EVSE). Another profile with the
  same EVSE, purpose and stackLevel (and, for TxProfile, the same transaction) whose validity period
  (validFrom..validTo) overlaps is a conflict: Rejected (UNVERIFIED, recalled K01.FR.39), not replaced as in 1.6.
- Only the first ``chargingSchedule`` is evaluated (it is the one without ISO 15118, which the simulator does not
  model); the others are kept and reported. Every schedule is validated.
- Rejected besides: a non-zero first ``startPeriod``, non-increasing ``startPeriod`` values, a negative
  ``limit`` / ``minChargingRate``, ``numberPhases`` outside 1..3, ``phaseToUse`` other than 1..3 or without
  ``numberPhases`` 1, ``duration <= 0``, Recurring without ``recurrencyKind``, ``validTo <= validFrom``, a
  negative stackLevel, an evseId outside ``0..evse_count``, and the Device Model limits the station passes in
  (SmartChargingCtrlr ProfileStackLevel / PeriodsPerSchedule / RateUnit, see :class:`Limits`).
- ClearChargingProfile: no fields clears every CSMS profile; ``chargingProfileId`` given -> the criteria are
  ignored; otherwise the given criteria are ANDed (as 1.6 errata 3.25 / 3.26). Nothing matched -> Unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Tuple

from .enums import ChargingProfileKindEnumType as ChargingProfileKind
from .enums import ChargingProfilePurposeEnumType as ChargingProfilePurpose
from .enums import ChargingRateUnitEnumType as ChargingRateUnit
from .enums import RecurrencyKindEnumType as RecurrencyKind

__all__ = ["ChargingProfile", "ChargingProfileKind", "ChargingProfilePurpose", "ChargingRateUnit",
           "ChargingSchedule", "ClearStatus", "InstalledProfile", "Limits", "ProfileStatus", "ProfileStore",
           "RecurrencyKind", "SchedulePeriod"]


class ProfileStatus(str, Enum):
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"


class ClearStatus(str, Enum):
    ACCEPTED = "Accepted"
    UNKNOWN = "Unknown"


@dataclass(frozen=True)
class SchedulePeriod:
    start_period: int  # seconds from the schedule origin
    limit: float  # in the schedule's unit; A = per phase
    number_phases: Optional[int] = None  # None -> 3
    phase_to_use: Optional[int] = None  # only with number_phases 1; not modelled by the power model


@dataclass(frozen=True)
class ChargingSchedule:
    id: int
    unit: ChargingRateUnit
    periods: Tuple[SchedulePeriod, ...]
    duration: Optional[int] = None  # seconds; None -> last period runs indefinitely
    start_schedule: Optional[datetime] = None
    min_charging_rate: Optional[float] = None


@dataclass(frozen=True)
class ChargingProfile:
    profile_id: int
    stack_level: int
    purpose: ChargingProfilePurpose
    kind: ChargingProfileKind
    #: The evaluated schedule: the first of the ``chargingSchedule`` array (see Interpretations).
    schedule: ChargingSchedule
    transaction_id: Optional[str] = None
    recurrency_kind: Optional[RecurrencyKind] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    #: The 2nd / 3rd ``chargingSchedule`` entries, kept for reporting only.
    other_schedules: Tuple[ChargingSchedule, ...] = ()


@dataclass(frozen=True)
class InstalledProfile:
    evse_id: int
    profile: ChargingProfile
    #: The 2.0.1 wire model it came from (``models.ChargingProfileType``), reported by GetChargingProfiles.
    wire: Any = field(default=None, compare=False, repr=False)

    @property
    def connector_id(self) -> int:
        """The Web API name of the EVSE id (one connector per EVSE)."""
        return self.evse_id


@dataclass(frozen=True)
class Limits:
    """SmartChargingCtrlr limits a profile must respect (``None`` = not checked)."""

    max_stack_level: Optional[int] = None
    max_periods: Optional[int] = None
    units: Optional[frozenset] = None  # allowed ChargingRateUnit values


def _schedule_ok(sched: ChargingSchedule, limits: Limits) -> bool:
    starts = [p.start_period for p in sched.periods]
    if not starts or starts[0] != 0 or any(b <= a for a, b in zip(starts, starts[1:])):
        return False
    if limits.max_periods is not None and len(starts) > limits.max_periods:
        return False
    if limits.units is not None and sched.unit not in limits.units:
        return False
    for period in sched.periods:
        if period.limit < 0:
            return False
        if period.number_phases is not None and not 1 <= period.number_phases <= 3:
            return False
        if period.phase_to_use is not None and (period.number_phases != 1 or not 1 <= period.phase_to_use <= 3):
            return False
    if sched.duration is not None and sched.duration <= 0:
        return False
    return sched.min_charging_rate is None or sched.min_charging_rate >= 0


def _profile_ok(profile: ChargingProfile, limits: Limits) -> bool:
    if not all(_schedule_ok(s, limits) for s in (profile.schedule, *profile.other_schedules)):
        return False
    if profile.kind is ChargingProfileKind.recurring and profile.recurrency_kind is None:
        return False
    if profile.valid_from and profile.valid_to and profile.valid_to <= profile.valid_from:
        return False
    if limits.max_stack_level is not None and profile.stack_level > limits.max_stack_level:
        return False
    return profile.stack_level >= 0


def _overlap(a: ChargingProfile, b: ChargingProfile) -> bool:
    """The validity periods [validFrom, validTo) of ``a`` and ``b`` overlap (open ends are infinite)."""
    if a.valid_to is not None and b.valid_from is not None and a.valid_to <= b.valid_from:
        return False
    return not (b.valid_to is not None and a.valid_from is not None and b.valid_to <= a.valid_from)


class ProfileStore:
    """Charging profiles installed on one station, keyed by evseId (0 = whole station)."""

    def __init__(self, evse_count: int) -> None:
        self.evse_count = evse_count
        self._installed: list[InstalledProfile] = []

    @property
    def connector_count(self) -> int:
        """EVSEs 1..N (the composite evaluator's name for them)."""
        return self.evse_count

    def profiles(self, evse_id: Optional[int] = None) -> Tuple[InstalledProfile, ...]:
        return tuple(ip for ip in self._installed if evse_id is None or ip.evse_id == evse_id)

    def _conflict(self, evse_id: int, profile: ChargingProfile) -> bool:
        return any(ip.evse_id == evse_id and ip.profile.profile_id != profile.profile_id
                   and ip.profile.purpose is profile.purpose and ip.profile.stack_level == profile.stack_level
                   and ip.profile.transaction_id == profile.transaction_id and _overlap(ip.profile, profile)
                   for ip in self._installed)

    def validate(
        self,
        evse_id: int,
        profile: ChargingProfile,
        *,
        active_transaction: Optional[str] = None,
        for_remote_start: bool = False,
        external: bool = False,
        limits: Limits = Limits(),
    ) -> ProfileStatus:
        """The checks of :meth:`install` without installing anything.

        ``for_remote_start``: the profile of a RequestStartTransaction, checked before its transaction exists
        (must be a TxProfile without ``transactionId``; ``active_transaction`` is ignored). ``external``: the
        operator's ChargingStationExternalConstraints (the only way to install that purpose).
        """
        purpose = profile.purpose
        if not 0 <= evse_id <= self.evse_count or not _profile_ok(profile, limits):
            return ProfileStatus.REJECTED
        if (purpose is ChargingProfilePurpose.charging_station_external_constraints) != external:
            return ProfileStatus.REJECTED
        if purpose is ChargingProfilePurpose.charging_station_max_profile and evse_id != 0:
            return ProfileStatus.REJECTED
        if for_remote_start:
            ok = purpose is ChargingProfilePurpose.tx_profile and evse_id != 0 and profile.transaction_id is None
            return ProfileStatus.ACCEPTED if ok else ProfileStatus.REJECTED
        if purpose is ChargingProfilePurpose.tx_profile:
            if evse_id == 0 or active_transaction is None or profile.transaction_id != active_transaction:
                return ProfileStatus.REJECTED
        if self._conflict(evse_id, profile):
            return ProfileStatus.REJECTED
        return ProfileStatus.ACCEPTED

    def install(
        self,
        evse_id: int,
        profile: ChargingProfile,
        *,
        active_transaction: Optional[str] = None,
        external: bool = False,
        limits: Limits = Limits(),
        wire: Any = None,
    ) -> ProfileStatus:
        """SetChargingProfile. ``active_transaction`` is the transactionId running on ``evse_id``."""
        if self.validate(evse_id, profile, active_transaction=active_transaction, external=external,
                         limits=limits) is ProfileStatus.REJECTED:
            return ProfileStatus.REJECTED
        ext = ChargingProfilePurpose.charging_station_external_constraints
        # The same id replaces, except across the CSMS / operator boundary (a CSMS id never hits an external one).
        self._installed = [ip for ip in self._installed
                           if ip.profile.profile_id != profile.profile_id or (ip.profile.purpose is ext) != external]
        self._installed.append(InstalledProfile(evse_id, profile, wire))
        return ProfileStatus.ACCEPTED

    def clear(
        self,
        *,
        profile_id: Optional[int] = None,
        evse_id: Optional[int] = None,
        purpose: Optional[ChargingProfilePurpose] = None,
        stack_level: Optional[int] = None,
    ) -> ClearStatus:
        """ClearChargingProfile; ``profile_id`` given -> the other filters are ignored. External constraints are
        never cleared here (see :meth:`clear_external`)."""

        def matches(ip: InstalledProfile) -> bool:
            if ip.profile.purpose is ChargingProfilePurpose.charging_station_external_constraints:
                return False
            if profile_id is not None:
                return ip.profile.profile_id == profile_id
            return (
                (evse_id is None or ip.evse_id == evse_id)
                and (purpose is None or ip.profile.purpose is purpose)
                and (stack_level is None or ip.profile.stack_level == stack_level)
            )

        kept = [ip for ip in self._installed if not matches(ip)]
        if len(kept) == len(self._installed):
            return ClearStatus.UNKNOWN
        self._installed = kept
        return ClearStatus.ACCEPTED

    def clear_external(self, evse_id: int) -> bool:
        """Remove the external constraints of ``evse_id``; ``False`` when there were none."""
        before = len(self._installed)
        self._installed = [
            ip for ip in self._installed
            if not (ip.evse_id == evse_id
                    and ip.profile.purpose is ChargingProfilePurpose.charging_station_external_constraints)
        ]
        return len(self._installed) != before

    def end_transaction(self, evse_id: int) -> int:
        """Drop the TxProfiles of ``evse_id`` when its transaction ends. Returns count."""
        before = len(self._installed)
        self._installed = [
            ip
            for ip in self._installed
            if not (ip.evse_id == evse_id and ip.profile.purpose is ChargingProfilePurpose.tx_profile)
        ]
        return before - len(self._installed)
