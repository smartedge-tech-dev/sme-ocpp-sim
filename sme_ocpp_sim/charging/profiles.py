"""Version-neutral charging profile domain types and the per-charger :class:`ProfileStore`.

Rules implemented from OCPP 1.6 edition 2 (and its errata sheet v4.0):

- 3.13.1: ChargePointMaxProfile only on connectorId 0; TxProfile only on connectorId > 0 and only while a
  transaction is active on that connector, otherwise it is discarded with an error status; TxProfiles cease
  to be valid when their transaction ends (7.10).
- 3.13.2 / 5.16.3 / 5.16.4: a new profile replaces an installed one with the same chargingProfileId, or with
  the same stackLevel and purpose.
- 7.13 / errata 3.45: the first period starts at 0. 7.8: stackLevel >= 0.
- 6.13 / errata 3.25-3.26: ClearChargingProfile with no fields clears everything; otherwise the given fields
  are ANDed; if ``id`` is given all other fields are ignored. Nothing matched -> Unknown (7.21).

Interpretations (not defined by the spec)
-----------------------------------------
- Same stackLevel + purpose replacement is scoped per connectorId (3.13.1 says a TxDefaultProfile for
  connector > 0 replaces the connector-0 one "only for that specific connector", so the scope cannot be the
  whole charger).
- A TxProfile whose ``transaction_id`` differs from the connector's active transaction is Rejected.
- Recurring without ``recurrency_kind``, non-increasing ``start_period`` values, an empty period list, a
  connectorId outside ``0..connector_count`` or ``valid_to <= valid_from`` are Rejected.
- Value ranges the 1.6 schemas do not carry are checked here (the 2.0.1 adapter reuses this store): a negative
  ``limit`` / ``start_period`` / ``min_charging_rate``, ``number_phases`` outside 1..3 and ``duration <= 0`` (a
  schedule of zero length never applies) are Rejected (7.13 / 7.14).
- RemoteStartTransaction profiles (:meth:`ProfileStore.validate` with ``for_remote_start``) must be TxProfiles
  without a ``transaction_id`` (6.33, 5.16.2); the transaction does not exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple


class ChargingProfilePurpose(str, Enum):
    CHARGE_POINT_MAX = "ChargePointMaxProfile"
    TX_DEFAULT = "TxDefaultProfile"
    TX = "TxProfile"


class ChargingProfileKind(str, Enum):
    ABSOLUTE = "Absolute"
    RECURRING = "Recurring"
    RELATIVE = "Relative"


class RecurrencyKind(str, Enum):
    DAILY = "Daily"
    WEEKLY = "Weekly"


class ChargingRateUnit(str, Enum):
    W = "W"
    A = "A"


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
    number_phases: Optional[int] = None  # None -> 3 (7.14)


@dataclass(frozen=True)
class ChargingSchedule:
    unit: ChargingRateUnit
    periods: Tuple[SchedulePeriod, ...]
    duration: Optional[int] = None  # seconds; None -> last period runs indefinitely (7.13)
    start_schedule: Optional[datetime] = None
    min_charging_rate: Optional[float] = None


@dataclass(frozen=True)
class ChargingProfile:
    profile_id: int
    stack_level: int
    purpose: ChargingProfilePurpose
    kind: ChargingProfileKind
    schedule: ChargingSchedule
    transaction_id: Optional[int] = None
    recurrency_kind: Optional[RecurrencyKind] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None


@dataclass(frozen=True)
class InstalledProfile:
    connector_id: int
    profile: ChargingProfile


def _schedule_ok(profile: ChargingProfile) -> bool:
    sched = profile.schedule
    starts = [p.start_period for p in sched.periods]
    if not starts or starts[0] != 0 or any(b <= a for a, b in zip(starts, starts[1:])):
        return False
    for period in sched.periods:
        if period.start_period < 0 or period.limit < 0:
            return False
        if period.number_phases is not None and not 1 <= period.number_phases <= 3:
            return False
    if sched.duration is not None and sched.duration <= 0:
        return False
    if sched.min_charging_rate is not None and sched.min_charging_rate < 0:
        return False
    if profile.kind is ChargingProfileKind.RECURRING and profile.recurrency_kind is None:
        return False
    if profile.valid_from and profile.valid_to and profile.valid_to <= profile.valid_from:
        return False
    return profile.stack_level >= 0


class ProfileStore:
    """Charging profiles installed on one charger, keyed by connectorId (0 = whole charger)."""

    def __init__(self, connector_count: int) -> None:
        self.connector_count = connector_count
        self._installed: list[InstalledProfile] = []

    def profiles(self, connector_id: Optional[int] = None) -> Tuple[InstalledProfile, ...]:
        return tuple(ip for ip in self._installed if connector_id is None or ip.connector_id == connector_id)

    def validate(
        self,
        connector_id: int,
        profile: ChargingProfile,
        *,
        active_transaction: Optional[int] = None,
        for_remote_start: bool = False,
    ) -> ProfileStatus:
        """The checks of :meth:`install` without installing anything.

        ``for_remote_start``: the profile of a RemoteStartTransaction, checked before its transaction exists
        (must be a TxProfile on a connector > 0 without ``transaction_id``; ``active_transaction`` is ignored).
        """
        purpose = profile.purpose
        if not 0 <= connector_id <= self.connector_count or not _schedule_ok(profile):
            return ProfileStatus.REJECTED
        if purpose is ChargingProfilePurpose.CHARGE_POINT_MAX and connector_id != 0:
            return ProfileStatus.REJECTED
        if for_remote_start:
            ok = purpose is ChargingProfilePurpose.TX and connector_id != 0 and profile.transaction_id is None
            return ProfileStatus.ACCEPTED if ok else ProfileStatus.REJECTED
        if purpose is ChargingProfilePurpose.TX:
            if connector_id == 0 or active_transaction is None:
                return ProfileStatus.REJECTED
            if profile.transaction_id is not None and profile.transaction_id != active_transaction:
                return ProfileStatus.REJECTED
        return ProfileStatus.ACCEPTED

    def install(
        self, connector_id: int, profile: ChargingProfile, *, active_transaction: Optional[int] = None
    ) -> ProfileStatus:
        """SetChargingProfile. ``active_transaction`` is the transactionId running on ``connector_id``."""
        purpose = profile.purpose
        if self.validate(connector_id, profile, active_transaction=active_transaction) is ProfileStatus.REJECTED:
            return ProfileStatus.REJECTED
        self._installed = [
            ip
            for ip in self._installed
            if ip.profile.profile_id != profile.profile_id
            and not (
                ip.connector_id == connector_id
                and ip.profile.stack_level == profile.stack_level
                and ip.profile.purpose is purpose
            )
        ]
        self._installed.append(InstalledProfile(connector_id, profile))
        return ProfileStatus.ACCEPTED

    def clear(
        self,
        *,
        profile_id: Optional[int] = None,
        connector_id: Optional[int] = None,
        purpose: Optional[ChargingProfilePurpose] = None,
        stack_level: Optional[int] = None,
    ) -> ClearStatus:
        """ClearChargingProfile; ``profile_id`` given -> the other filters are ignored (errata 3.26)."""

        def matches(ip: InstalledProfile) -> bool:
            if profile_id is not None:
                return ip.profile.profile_id == profile_id
            return (
                (connector_id is None or ip.connector_id == connector_id)
                and (purpose is None or ip.profile.purpose is purpose)
                and (stack_level is None or ip.profile.stack_level == stack_level)
            )

        kept = [ip for ip in self._installed if not matches(ip)]
        if len(kept) == len(self._installed):
            return ClearStatus.UNKNOWN
        self._installed = kept
        return ClearStatus.ACCEPTED

    def end_transaction(self, connector_id: int) -> int:
        """Drop the TxProfiles of ``connector_id`` when its transaction ends (3.13.1, 7.10). Returns count."""
        before = len(self._installed)
        self._installed = [
            ip
            for ip in self._installed
            if not (ip.connector_id == connector_id and ip.profile.purpose is ChargingProfilePurpose.TX)
        ]
        return before - len(self._installed)
