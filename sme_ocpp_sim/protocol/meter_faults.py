"""Meter value impairments of one connector (``impairments.meter``, v0.3 M2, simulator-only).

Shared by the version stations: :meth:`MeterFaults.drop` decides whether a periodic meter message is left out,
:meth:`MeterFaults.hold` / :meth:`MeterFaults.flush` reorder them (``reorder_window``) and :meth:`MeterFaults.noisy`
adds Voltage noise. A station keeps one object per connector through :func:`meter_faults`; it is rebuilt (the
held messages carried over) when the charger's impairments change, so a switch applies without a reconnect.

Each connector draws from its own seeded streams (``meter:{cid}``, ``reorder:{cid}``, ``noise:{cid}`` of
:func:`..rpc.impair.make_rng`), so one connector's traffic never shifts another's pattern.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Sequence

from ..config import ImpairmentConfig
from ..rpc.impair import impairment_seed, make_rng
from .base import Sample

#: The impairments of a host without any (test hosts).
NO_IMPAIRMENTS = ImpairmentConfig()

VOLTAGE = "Voltage"
VOLTAGE_DECIMALS = 1


def impairments_of(host: Any) -> ImpairmentConfig:
    """The live ``impairments`` of a station host (none when the host has no such attribute)."""
    imp = getattr(host, "impairments", None)
    return imp if isinstance(imp, ImpairmentConfig) else NO_IMPAIRMENTS


def _key(imp: ImpairmentConfig) -> tuple:
    return imp.seed, imp.meter.model_dump_json()


class MeterFaults:
    def __init__(self, imp: ImpairmentConfig, charger_id: str, connector_id: int) -> None:
        self.key = _key(imp)
        self.cfg = imp.meter
        seed = impairment_seed(imp.seed, charger_id)
        self._drop = make_rng(seed, f"meter:{connector_id}")
        self._order = make_rng(seed, f"reorder:{connector_id}")
        self._noise = make_rng(seed, f"noise:{connector_id}")
        self.held: List[Any] = []

    def drop(self) -> bool:
        """True when this periodic meter message is to be left out (``drop_ratio``)."""
        return self.cfg.drop_ratio > 0 and self._drop.random() < self.cfg.drop_ratio

    def hold(self, item: Any) -> List[Any]:
        """Hold ``item``; returns the messages to send now (all held ones, shuffled, once the window is full)."""
        self.held.append(item)
        return self.flush() if len(self.held) >= max(1, self.cfg.reorder_window) else []

    def flush(self) -> List[Any]:
        """The held messages in a random order (the window is full, or the transaction ends)."""
        out, self.held = self.held, []
        if len(out) > 1:
            self._order.shuffle(out)
        return out

    def noisy(self, samples: Sequence[Sample]) -> List[Sample]:
        """``samples`` with +- ``voltage_noise_v`` (uniform) on every Voltage."""
        v = self.cfg.voltage_noise_v
        if v <= 0:
            return list(samples)
        return [replace(s, value=round(s.value + self._noise.uniform(-v, v), VOLTAGE_DECIMALS))
                if s.measurand == VOLTAGE else s for s in samples]


def meter_faults(cache: Dict[int, MeterFaults], host: Any, charger_id: str, connector_id: int) -> MeterFaults:
    """The connector's :class:`MeterFaults` in ``cache``, rebuilt when the meter impairments or the seed changed."""
    imp = impairments_of(host)
    cur = cache.get(connector_id)
    if cur is None or cur.key != _key(imp):
        new = MeterFaults(imp, charger_id, connector_id)
        if cur is not None:
            new.held = cur.held  # a changed window applies to the messages already held
        cache[connector_id] = cur = new
    return cur
