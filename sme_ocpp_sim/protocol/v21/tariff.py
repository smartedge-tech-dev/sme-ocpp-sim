"""OCPP 2.1 transaction limits (E16 / F07) and the rough local cost calculation (I12), as plain functions.

Cost (2.1 plan Q19: the energy price only, documented as an approximation)
--------------------------------------------------------------------------
- The energy cost is the transaction's imported energy times ``energy.prices[0].priceKwh`` of the tariff in force.
  A tariff change (ChangeTransactionTariff, I11.FR.08) applies from that moment on: the energy before it keeps the
  price of the previous tariff (no retroactive calculation).
- ``fixedFee.prices[0].priceFixed`` of the tariff in force is added once (I12.FR.05).
- ``minCost`` / ``maxCost`` (``exclTax``) clamp the total; ``typeOfCost`` is then ``MinCost`` / ``MaxCost``.
- Not calculated: chargingTime, idleTime, reservationTime / reservationFixed prices, conditions (tariffs with
  conditions are refused, TariffCostCtrlr.ConditionsSupported is false), taxes (only ``exclTax`` is reported, the
  optional ``inclTax`` is left out) and ``chargingPeriods``. ``totalUsage`` reports the energy, the charging time
  (power flowing) and the idle time (no power) of the transaction.

Limits (E16)
------------
``maxEnergy`` (Wh imported in the transaction), ``maxTime`` (s since the transaction started, E16.FR.09),
``maxSoC`` (%) and ``maxCost`` (in the tariff currency) are reached when the actual value is at or above the limit;
a limit already exceeded when it is set is reached at once (E16.FR.10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

from . import models as m

#: TransactionLimitType field (model attribute) -> its wire name in TxCtrlr.SupportedLimits.
LIMIT_FIELDS: Dict[str, str] = {"max_cost": "maxCost", "max_energy": "maxEnergy", "max_time": "maxTime",
                                "max_so_c": "maxSoC"}
#: The order in which reached limits are reported when several are reached at once.
LIMIT_ORDER = ("max_cost", "max_energy", "max_time", "max_so_c")

_PRICED = ("energy", "charging_time", "idle_time", "fixed_fee", "reservation_time", "reservation_fixed")


def limits_of(limit: Optional[m.TransactionLimitType], supported: Iterable[str]) -> Dict[str, float]:
    """The values of ``limit`` (a TransactionLimitType) whose wire names are in ``supported``."""
    if limit is None:
        return {}
    wanted = set(supported)
    return {k: getattr(limit, k) for k, wire in LIMIT_FIELDS.items()
            if wire in wanted and getattr(limit, k) is not None}


def reached(limits: Dict[str, float], *, energy_wh: float, elapsed_s: float, soc_pct: Optional[float],
            cost: Optional[float]) -> Optional[str]:
    """The first limit of ``limits`` (LIMIT_ORDER) that the actual values reach, ``None`` when none does. A cost
    limit without a known cost, or a SoC limit without a SoC, is not reached."""
    actual = {"max_cost": cost, "max_energy": energy_wh, "max_time": elapsed_s, "max_so_c": soc_pct}
    for name in LIMIT_ORDER:
        if name in limits and actual[name] is not None and actual[name] >= limits[name]:
            return name
    return None


def too_many_elements(tariff: m.TariffType, max_elements: int) -> bool:
    """I07.FR.02 / I11.FR.02: a price list of the tariff is longer than TariffCostCtrlr.MaxElements."""
    return any(len(getattr(tariff, k).prices) > max_elements for k in _PRICED if getattr(tariff, k) is not None)


def has_conditions(tariff: m.TariffType) -> bool:
    """I07.FR.03 / I11.FR.03: the tariff has TariffConditionsType / TariffConditionsFixedType elements."""
    return any(p.conditions is not None for k in _PRICED if getattr(tariff, k) is not None
               for p in getattr(tariff, k).prices)


def _energy_price(tariff: Optional[m.TariffType]) -> float:
    return tariff.energy.prices[0].price_kwh if tariff is not None and tariff.energy is not None else 0.0


@dataclass
class CostMeter:
    """The running cost of one transaction under its tariff(s); ``energy_wh`` is the energy of the transaction."""

    tariff: Optional[m.TariffType]
    start_wh: float = 0.0  # transaction energy when ``tariff`` came into force
    base: float = 0.0  # energy cost under the previous tariffs
    charging_s: float = 0.0
    idle_s: float = 0.0
    #: Cost reported by the CSMS (TransactionEventResponse.totalCost), used without a local tariff (E16.FR.15).
    central: Optional[float] = field(default=None)

    def switch(self, tariff: m.TariffType, energy_wh: float) -> None:
        """A new tariff from now on (I11.FR.08)."""
        self.base = self._energy_cost(energy_wh)
        self.tariff, self.start_wh = tariff, energy_wh

    def _energy_cost(self, energy_wh: float) -> float:
        return self.base + max(0.0, energy_wh - self.start_wh) / 1000.0 * _energy_price(self.tariff)

    def _fixed(self) -> float:
        t = self.tariff
        return t.fixed_fee.prices[0].price_fixed if t is not None and t.fixed_fee is not None else 0.0

    def total(self, energy_wh: float) -> Tuple[float, str]:
        """(total excluding tax, typeOfCost) by the local tariff."""
        total, kind = self._energy_cost(energy_wh) + self._fixed(), "NormalCost"
        t = self.tariff
        if t is not None and t.min_cost is not None and t.min_cost.excl_tax is not None and total < t.min_cost.excl_tax:
            total, kind = t.min_cost.excl_tax, "MinCost"
        if t is not None and t.max_cost is not None and t.max_cost.excl_tax is not None and total > t.max_cost.excl_tax:
            total, kind = t.max_cost.excl_tax, "MaxCost"
        return round(total, 4), kind

    def cost(self, energy_wh: float, local: bool) -> Optional[float]:
        """The cost a cost limit is compared with: local (E16.FR.16) or the CSMS's (E16.FR.15)."""
        if local and self.tariff is not None:
            return self.total(energy_wh)[0]
        return self.central

    def details(self, energy_wh: float, models: Any = m) -> m.CostDetailsType:
        """CostDetailsType of the final TransactionEvent (I12.FR.01); the tariff in force must be set."""
        assert self.tariff is not None
        total, kind = self.total(energy_wh)
        P = models.PriceType
        fixed = self._fixed()
        return models.CostDetailsType(
            total_cost=models.TotalCostType(
                currency=self.tariff.currency, type_of_cost=kind,
                energy=P(excl_tax=round(self._energy_cost(energy_wh), 4)),
                fixed=P(excl_tax=fixed) if self.tariff.fixed_fee is not None else None,
                total=models.TotalPriceType(excl_tax=total)),
            total_usage=models.TotalUsageType(energy=round(energy_wh, 3), charging_time=int(self.charging_s),
                                              idle_time=int(self.idle_s)))
