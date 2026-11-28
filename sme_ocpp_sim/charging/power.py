"""Power model step (design §3.2): EVSE limit + EV acceptance -> delivered power, energy register, SoC.

Pure and deterministic: the caller owns :class:`PowerState` and feeds it back each tick.

Modelling choices (simulator conventions, not defined by OCPP):

- ``target = min(evse_max_w, evse_limit_w, ev_accept_w)``; ``evse_limit_w=None`` means "no schedule limit".
- Suspension (the cause, reported independently of the ramp):
  ``SuspendedEV`` if the EV accepts 0 W (target SoC reached, or a zero taper) or its acceptance has tapered
  below its own minimum usable power (``ev.min_current_a`` per used phase; end-of-charge taper), checked first;
  otherwise ``SuspendedEVSE`` if the EVSE offer is 0 W or below that minimum -- the EV cannot run on it.
  Either way the target becomes 0 W.
- Ramp: power moves toward the target at ``ramp_up_w_per_s`` / ``ramp_down_w_per_s`` (``None`` = step
  change). Default ramp-down is a step change, so a lowered limit is honoured within the same tick.
- ``time_scale`` multiplies the caller's elapsed ``dt_s`` into simulated seconds; *everything* time-based
  in this module (ramp, energy, SoC) advances by ``dt_s * time_scale``.
- Energy uses the trapezoid of the previous and new power over the step; the register only grows.
  SoC += energy * efficiency / capacity; it is not clipped at the target, so it may overshoot it by at
  most one step's worth (then the EV accepts 0 W).
- Electrical: ``P = V_phase * I * phases``. ``AcSupply.voltage_v`` is the nominal supply voltage; with
  ``line_to_line=True`` (e.g. Japanese 3-phase 200 V delta) the per-phase voltage on a 3-phase supply is
  ``voltage_v / sqrt(3)``, so ``P = sqrt(3) * V_LL * I``. Single-phase always uses ``voltage_v``.
  Phases in use = ``min(ev.phases, supply.phases)``; current is balanced across them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal, Optional

from .ev import EvModel, ev_accept_w, ev_min_power_w

Suspension = Literal["SuspendedEV", "SuspendedEVSE"]


@dataclass(frozen=True)
class AcSupply:
    voltage_v: float = 200.0
    phases: int = 1
    line_to_line: bool = False

    def __post_init__(self) -> None:
        if self.voltage_v <= 0:
            raise ValueError("voltage_v must be > 0")
        if self.phases not in (1, 2, 3):
            raise ValueError("phases must be 1, 2 or 3")

    def phase_voltage_v(self) -> float:
        """Voltage across each phase load used in ``P = V_phase * I * phases``."""
        if self.line_to_line and self.phases == 3:
            return self.voltage_v / math.sqrt(3)
        return self.voltage_v

    def phases_used(self, ev: EvModel) -> int:
        return min(ev.phases, self.phases)


def amps_to_watts(current_a: float, supply: AcSupply, phases: int) -> float:
    """Per-phase current limit (e.g. a profile in A) -> active power over ``phases`` phases."""
    return current_a * supply.phase_voltage_v() * phases


def watts_to_amps(power_w: float, supply: AcSupply, phases: int) -> float:
    """Active power -> balanced per-phase current over ``phases`` phases."""
    return power_w / (supply.phase_voltage_v() * phases)


@dataclass(frozen=True)
class PowerState:
    soc_pct: float
    power_w: float = 0.0
    energy_wh: float = 0.0          # Energy.Active.Import.Register, monotonic
    target_w: float = 0.0           # power the step was heading for (before the ramp)
    suspension: Optional[Suspension] = None


def _ramp(current_w: float, target_w: float, sim_dt_s: float,
          up_w_per_s: Optional[float], down_w_per_s: Optional[float]) -> float:
    rate = up_w_per_s if target_w > current_w else down_w_per_s
    if rate is None:
        return target_w
    max_delta = rate * sim_dt_s
    return current_w + max(-max_delta, min(max_delta, target_w - current_w))


def step(state: PowerState, *, dt_s: float, ev: EvModel, target_soc_pct: float,
         evse_limit_w: Optional[float], evse_max_w: float, supply: AcSupply,
         ramp_up_w_per_s: Optional[float] = None, ramp_down_w_per_s: Optional[float] = None,
         time_scale: float = 1.0) -> PowerState:
    """Advance one tick of ``dt_s`` caller seconds (``dt_s * time_scale`` simulated seconds)."""
    if dt_s < 0 or time_scale <= 0:
        raise ValueError("dt_s must be >= 0 and time_scale > 0")
    for rate in (ramp_up_w_per_s, ramp_down_w_per_s):
        if rate is not None and rate <= 0:
            raise ValueError("ramp rates must be > 0 or None")
    sim_dt_s = dt_s * time_scale
    v_phase = supply.phase_voltage_v()
    n = supply.phases_used(ev)

    offer_w = evse_max_w if evse_limit_w is None else min(evse_max_w, evse_limit_w)
    offer_w = max(0.0, offer_w)
    accept_w = ev_accept_w(ev, state.soc_pct, target_soc_pct, v_phase, n)

    min_w = ev_min_power_w(ev, v_phase, n)
    suspension: Optional[Suspension] = None
    if accept_w <= 0 or accept_w < min_w:
        suspension, target_w = "SuspendedEV", 0.0
    elif offer_w <= 0 or offer_w < min_w:
        suspension, target_w = "SuspendedEVSE", 0.0
    else:
        target_w = min(offer_w, accept_w)

    power_w = _ramp(state.power_w, target_w, sim_dt_s, ramp_up_w_per_s, ramp_down_w_per_s)
    delta_wh = (state.power_w + power_w) / 2 * sim_dt_s / 3600.0
    soc_pct = min(100.0, state.soc_pct + delta_wh * ev.efficiency / (ev.capacity_kwh * 10.0))
    return replace(state, soc_pct=soc_pct, power_w=power_w, energy_wh=state.energy_wh + delta_wh,
                   target_w=target_w, suspension=suspension)


@dataclass(frozen=True)
class MeterReading:
    """Values for the 1.6 measurands Power.Active.Import (W), Current.Import (A, per phase),
    Voltage (V), Energy.Active.Import.Register (Wh) and SoC (Percent)."""

    power_active_import_w: float
    current_import_a: float          # per phase, balanced over ``phases``
    voltage_v: float                 # nominal supply voltage as configured
    energy_active_import_register_wh: float
    soc_pct: float
    phases: int


def meter_reading(state: PowerState, ev: EvModel, supply: AcSupply) -> MeterReading:
    n = supply.phases_used(ev)
    return MeterReading(
        power_active_import_w=state.power_w,
        current_import_a=watts_to_amps(state.power_w, supply, n),
        voltage_v=supply.voltage_v,
        energy_active_import_register_wh=state.energy_wh,
        soc_pct=state.soc_pct,
        phases=n,
    )


#: Per-phase reporting of Voltage / Current.Import (simulator option ``meter.phase_values``):
#: ``off`` = one value without a phase, ``L-N`` = line-to-neutral voltages, ``L-L`` = line-to-line voltages.
PhaseValues = Literal["off", "L-N", "L-L"]

_PHASES = ("L1", "L2", "L3")
_LINE_TO_LINE = ("L1-L2", "L2-L3", "L3-L1")
VOLTAGE_DECIMALS = 1


def phase_voltages(supply: AcSupply, mode: PhaseValues) -> list:
    """``[(phase, volts)]`` for every supply phase (OCPP: all possible phases are reported); ``[]`` when ``mode``
    is ``off``. ``L-N`` reports ``L1-N`` .. ``Ln-N`` with :meth:`AcSupply.phase_voltage_v`; ``L-L`` on a 3-phase
    supply reports ``L1-L2`` / ``L2-L3`` / ``L3-L1`` with ``sqrt(3)`` times that voltage. A supply with fewer than
    3 phases has no line-to-line pair of its own, so ``L-L`` falls back to ``L-N`` there."""
    if mode == "off":
        return []
    v_ln = supply.phase_voltage_v()
    if mode == "L-L" and supply.phases == 3:
        return [(p, round(v_ln * math.sqrt(3), VOLTAGE_DECIMALS)) for p in _LINE_TO_LINE]
    return [(f"{p}-N", round(v_ln, VOLTAGE_DECIMALS)) for p in _PHASES[:supply.phases]]


def phase_currents(current_a: float, supply: AcSupply, phases_used: int) -> list:
    """``[(phase, amps)]`` for every supply phase: the balanced current on the ``phases_used`` phases the EV draws
    on (from ``L1``), 0 on the others."""
    return [(p, current_a if i < phases_used else 0.0) for i, p in enumerate(_PHASES[:supply.phases])]
