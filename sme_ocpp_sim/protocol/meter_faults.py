"""Meter value impairments of one connector (``impairments.meter``, v0.3 M2, simulator-only).

Shared by the version stations: :meth:`MeterFaults.drop` decides whether a periodic meter message is left out,
:meth:`MeterFaults.hold` / :meth:`MeterFaults.flush` reorder them (``reorder_window``) and :meth:`MeterFaults.noisy`
adds Voltage noise. A station keeps one object per connector through :func:`meter_faults`. A live edit of the
impairments only swaps its settings: the random streams (and the held messages) go on, so editing one field
never resets the others; a changed ``seed`` restarts the streams. A charger restart (a config change that
rebuilds the charger) builds a new station, so the streams start over and held messages are lost.

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


class MeterFaults:
    def __init__(self, imp: ImpairmentConfig, charger_id: str, connector_id: int) -> None:
        self.held: List[Any] = []
        #: Periodic meter messages left out so far (``drop_ratio``).
        self.dropped = 0
        self._ids = (charger_id, connector_id)
        self._seed: Any = object()
        self.apply(imp)

    def apply(self, imp: ImpairmentConfig) -> None:
        """Take the settings of ``imp``; the random streams restart only when the seed changed."""
        self.src, self.cfg = imp, imp.meter
        if imp.seed != self._seed:
            self._seed = imp.seed
            charger_id, cid = self._ids
            seed = impairment_seed(imp.seed, charger_id)
            self._drop = make_rng(seed, f"meter:{cid}")
            self._order = make_rng(seed, f"reorder:{cid}")
            self._noise = make_rng(seed, f"noise:{cid}")

    @property
    def releases(self) -> bool:
        """True when messages are held although the window no longer holds any (it shrank to 0 / 1)."""
        return bool(self.held) and self.cfg.reorder_window <= 1

    def drop(self) -> bool:
        """True when this periodic meter message is to be left out (``drop_ratio``)."""
        if self.cfg.drop_ratio > 0 and self._drop.random() < self.cfg.drop_ratio:
            self.dropped += 1
            return True
        return False

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
    """The connector's :class:`MeterFaults` in ``cache``, updated in place when the host's impairments object
    changed (``set_impairments`` installs a new one; the same object is not compared field by field)."""
    imp = impairments_of(host)
    cur = cache.get(connector_id)
    if cur is None:
        cache[connector_id] = cur = MeterFaults(imp, charger_id, connector_id)
    elif cur.src is not imp:
        cur.apply(imp)
    return cur
