"""EV model: battery, AC acceptance limits and a CC-CV-like SoC taper (pure data + functions).

Modelling choices (simulator conventions, not defined by OCPP):

- The taper is a piecewise-linear table ``SoC% -> fraction of max AC power`` (design §3.2/§7, e.g.
  ``[[0, 1.0], [80, 1.0], [100, 0.1]]`` = constant power to 80 %, then linear convergence). SoC outside the
  table is clamped to the first / last point.
- AC acceptance is ``min(max_ac_w * fraction, max_current_a * V_phase * phases_used)`` where
  ``phases_used = min(ev.phases, supply phases)``; the EV accepts 0 W once ``soc_pct >= target_soc_pct``.
- ``min_current_a`` (default 6 A, the lowest current an IEC 61851-1 control pilot can signal) is the least
  per-phase current the on-board charger can run on; :mod:`.power` treats a lower EVSE offer as no offer.
- ``efficiency`` is the grid-to-battery efficiency used to convert metered energy into SoC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

Curve = Tuple[Tuple[float, float], ...]

#: Constant power up to 80 % SoC, then linear down to 10 % of max at 100 % (design §7 example).
DEFAULT_CURVE: Curve = ((0.0, 1.0), (80.0, 1.0), (100.0, 0.1))


@dataclass(frozen=True)
class EvModel:
    id: str
    capacity_kwh: float
    max_ac_w: float
    max_current_a: float
    phases: int = 1
    min_current_a: float = 6.0
    efficiency: float = 0.9
    curve: Curve = DEFAULT_CURVE

    def __post_init__(self) -> None:
        if self.capacity_kwh <= 0 or self.max_ac_w <= 0 or self.max_current_a <= 0:
            raise ValueError("capacity_kwh, max_ac_w and max_current_a must be > 0")
        if self.phases not in (1, 2, 3):
            raise ValueError("phases must be 1, 2 or 3")
        if not 0 <= self.min_current_a <= self.max_current_a:
            raise ValueError("min_current_a must be within [0, max_current_a]")
        if not 0 < self.efficiency <= 1:
            raise ValueError("efficiency must be in (0, 1]")
        validate_curve(self.curve)


def validate_curve(curve: Curve) -> None:
    """Require >= 1 point, strictly increasing SoC within [0, 100] and fractions within [0, 1]."""
    if not curve:
        raise ValueError("curve needs at least one point")
    prev = -1.0
    for soc, frac in curve:
        if not 0 <= soc <= 100 or not 0 <= frac <= 1:
            raise ValueError(f"curve point out of range: ({soc}, {frac})")
        if soc <= prev:
            raise ValueError("curve SoC values must be strictly increasing")
        prev = soc


def curve_fraction(curve: Curve, soc_pct: float) -> float:
    """Linear interpolation of the taper table at ``soc_pct`` (clamped to the table ends)."""
    if soc_pct <= curve[0][0]:
        return curve[0][1]
    for (s0, f0), (s1, f1) in zip(curve, curve[1:]):
        if soc_pct <= s1:
            return f0 + (f1 - f0) * (soc_pct - s0) / (s1 - s0)
    return curve[-1][1]


def ev_max_power_w(ev: EvModel, soc_pct: float, phase_voltage_v: float, phases_used: int) -> float:
    """Power the EV can take at ``soc_pct`` (taper and current limit; ignores the target SoC)."""
    by_current = ev.max_current_a * phase_voltage_v * phases_used
    return min(ev.max_ac_w * curve_fraction(ev.curve, soc_pct), by_current)


def ev_accept_w(ev: EvModel, soc_pct: float, target_soc_pct: float,
                phase_voltage_v: float, phases_used: int) -> float:
    """EV acceptance: 0 once the target SoC is reached, otherwise :func:`ev_max_power_w`."""
    if soc_pct >= target_soc_pct:
        return 0.0
    return ev_max_power_w(ev, soc_pct, phase_voltage_v, phases_used)


def ev_min_power_w(ev: EvModel, phase_voltage_v: float, phases_used: int) -> float:
    """Lowest power the on-board charger can run at (``min_current_a`` on every used phase)."""
    return ev.min_current_a * phase_voltage_v * phases_used


@dataclass(frozen=True)
class Vehicle:
    """The EV of one charging session: its model plus the session's start and target SoC (%)."""

    ev: EvModel
    initial_soc_pct: float
    target_soc_pct: float


#: Illustrative, abstract presets: vehicle classes, not specific vehicles (no real-car data is shipped).
#: ``compact-40kwh`` mirrors the design §7 example. Their pool weights and initial SoC distributions are in
#: :data:`.vehicles.PRESET_PROFILES`.
PRESETS: Dict[str, EvModel] = {
    m.id: m
    for m in (
        EvModel("kei-20kwh", capacity_kwh=20, max_ac_w=3000, max_current_a=15, phases=1),
        EvModel("compact-40kwh", capacity_kwh=40, max_ac_w=6000, max_current_a=30, phases=1),
        EvModel("midsize-60kwh", capacity_kwh=60, max_ac_w=11000, max_current_a=16, phases=3),
        EvModel("suv-90kwh", capacity_kwh=90, max_ac_w=11000, max_current_a=16, phases=3),
        EvModel("phev-12kwh", capacity_kwh=12, max_ac_w=3200, max_current_a=16, phases=1),
        EvModel("van-75kwh", capacity_kwh=75, max_ac_w=11000, max_current_a=16, phases=3, efficiency=0.88),
    )
}
