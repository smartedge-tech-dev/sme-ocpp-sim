"""Vehicle profile pool (v0.3 M1): weighted, seeded selection of the EV that arrives at a connector.

Pure data + functions (no config, engine or protocol import), so the config, the charger runtime and the
traffic generator can all use it.

- A :class:`VehiclePool` is the built-in presets (:data:`.ev.PRESETS`, with :data:`PRESET_PROFILES`) plus the
  ``ev_models`` entries; an entry with a preset's id replaces that preset. Each entry has a ``weight`` (0 = never
  drawn) and an initial / target SoC distribution (:class:`SocSpec`).
- :meth:`VehiclePool.draw` picks one entry by weight and samples its SoC values, all from the caller's
  ``random.Random``. :func:`stream_rng` gives the per-connector stream ``vehicle:{charger}:{connector}`` of a
  seed, so the same seed gives the same vehicle sequence on every connector, independent of the others.
- SoC distributions: ``fixed`` (``mean``), ``uniform`` on ``[min, max]``, ``normal`` (``mean``, ``sd``) truncated
  to ``[min, max]`` (inverse-CDF sampling: no rejection loop). Values are rounded to 0.01 %.
- Stream position: every draw consumes exactly three random numbers (entry, initial SoC, target SoC; a ``fixed``
  value consumes one too), so changing an entry's distribution changes only the values drawn from it, never the
  later draws on that connector. Changing weights or the set of entries changes which entries are drawn.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from statistics import NormalDist
from typing import Dict, Iterable, Mapping, Optional, Tuple

from .ev import PRESETS, EvModel, Vehicle

DISTS = ("fixed", "uniform", "normal")


@dataclass(frozen=True)
class SocSpec:
    """A SoC (%) distribution; see the module docstring."""

    dist: str = "fixed"
    min: float = 0.0
    max: float = 100.0
    mean: Optional[float] = None
    sd: Optional[float] = None

    def __post_init__(self) -> None:
        if self.dist not in DISTS:
            raise ValueError(f"dist must be one of {', '.join(DISTS)}")
        if not 0 <= self.min <= self.max <= 100:
            raise ValueError("SoC distribution needs 0 <= min <= max <= 100")
        if self.dist in ("fixed", "normal"):
            if self.mean is None:
                raise ValueError(f"dist {self.dist} needs mean")
            if not self.min <= self.mean <= self.max:
                raise ValueError("mean must be within [min, max]")
        elif self.mean is not None:
            raise ValueError("dist uniform takes no mean (use min / max)")
        if self.dist == "normal":
            if self.sd is None or self.sd < 0:
                raise ValueError("dist normal needs sd >= 0")
        elif self.sd is not None:
            raise ValueError(f"dist {self.dist} takes no sd")

    @classmethod
    def fixed(cls, value: float) -> "SocSpec":
        return cls("fixed", mean=value)

    def sample(self, rng: random.Random) -> float:
        u = rng.random()  # exactly one number per value, whatever the distribution (module docstring)
        if self.dist == "fixed":
            return float(self.mean)  # type: ignore[arg-type]
        if self.dist == "uniform":
            return round(self.min + (self.max - self.min) * u, 2)
        if not self.sd:
            return float(self.mean)  # type: ignore[arg-type]
        nd = NormalDist(self.mean, self.sd)  # type: ignore[arg-type]
        lo, hi = nd.cdf(self.min), nd.cdf(self.max)
        p = lo + (hi - lo) * u
        x = nd.inv_cdf(p) if 0 < p < 1 else float(self.mean)  # type: ignore[arg-type]
        return round(min(max(x, self.min), self.max), 2)


@dataclass(frozen=True)
class PoolEntry:
    ev: EvModel
    weight: float
    initial_soc: SocSpec
    target_soc: SocSpec

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError(f"vehicle {self.ev.id!r}: weight must be >= 0")


@dataclass(frozen=True)
class VehiclePool:
    entries: Tuple[PoolEntry, ...]

    def __post_init__(self) -> None:
        ids = [e.ev.id for e in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate vehicle id in the pool")
        if sum(e.weight for e in self.entries) <= 0:
            raise ValueError("vehicle pool: the total weight must be > 0")

    @property
    def ids(self) -> Tuple[str, ...]:
        return tuple(e.ev.id for e in self.entries)

    @property
    def default_ev(self) -> EvModel:
        """The first drawable model (shown while no session runs)."""
        return next(e.ev for e in self.entries if e.weight > 0)

    def weighted(self, weights: Mapping[str, float]) -> "VehiclePool":
        """Only the given ids, with these weights (an explicit ``traffic.vehicles`` list); unknown id = error."""
        by_id = {e.ev.id: e for e in self.entries}
        unknown = sorted(set(weights) - set(by_id))
        if unknown:
            raise ValueError(f"unknown vehicle id(s) {unknown} (pool: {sorted(by_id)})")
        return VehiclePool(tuple(replace(by_id[i], weight=float(w)) for i, w in weights.items()))

    def draw(self, rng: random.Random) -> Vehicle:
        """One vehicle: an entry by weight, then its initial and target SoC (in this order, from ``rng``)."""
        e = rng.choices(self.entries, weights=[x.weight for x in self.entries])[0]
        return Vehicle(e.ev, e.initial_soc.sample(rng), e.target_soc.sample(rng))


def stream_rng(seed: int, charger_id: str, connector_id: int) -> random.Random:
    """The RNG stream ``vehicle:{charger_id}:{connector_id}`` of ``seed`` (str seeding is stable across runs)."""
    return random.Random(f"{seed}:vehicle:{charger_id}:{connector_id}")


#: Pool defaults of the presets: weight and initial SoC distribution (illustrative; the target SoC is the
#: charger's ``session.target_soc``). An ``ev_models`` entry with the same id replaces the preset completely.
PRESET_PROFILES: Dict[str, Tuple[float, SocSpec]] = {
    "kei-20kwh": (2.0, SocSpec("uniform", 10, 60)),
    "compact-40kwh": (3.0, SocSpec("uniform", 10, 60)),
    "midsize-60kwh": (2.0, SocSpec("uniform", 10, 60)),
    "suv-90kwh": (1.0, SocSpec("normal", 10, 80, mean=40, sd=15)),
    "phev-12kwh": (1.0, SocSpec("uniform", 0, 40)),
    "van-75kwh": (1.0, SocSpec("normal", 5, 70, mean=30, sd=10)),
}

#: ``(model, weight, initial SoC or None, target SoC or None)``; ``None`` = the charger's session value.
Custom = Tuple[EvModel, float, Optional[SocSpec], Optional[SocSpec]]


def build_pool(custom: Iterable[Custom], *, initial_soc_pct: float, target_soc_pct: float) -> VehiclePool:
    """Presets (in :data:`.ev.PRESETS` order) then the ``custom`` entries; a custom entry with a preset's id
    takes the preset's place. A missing SoC falls back to the session's fixed value."""
    target = SocSpec.fixed(target_soc_pct)
    out: Dict[str, PoolEntry] = {
        pid: PoolEntry(ev, PRESET_PROFILES[pid][0], PRESET_PROFILES[pid][1], target) for pid, ev in PRESETS.items()}
    for ev, weight, init, tgt in custom:
        out[ev.id] = PoolEntry(ev, weight, init or SocSpec.fixed(initial_soc_pct), tgt or target)
    return VehiclePool(tuple(out.values()))
