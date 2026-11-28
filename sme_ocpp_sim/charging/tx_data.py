"""Readings collected during a transaction for its end message (OCPP 1.6 StopTransaction.req ``transactionData``,
OCPP 2.0.1 TransactionEvent ``Ended`` ``meterValue``).

Version-neutral scheduling and buffering only; which measurands are sampled and how the readings go on the wire
stays in each version's station.

- A *sampled* stream takes a reading every ``interval`` seconds from the start of the transaction (1.6
  StopTxnSampledData / MeterValueSampleInterval, 2.0.1 SampledDataCtrlr.TxEndedInterval). Monotonic seconds.
- An *aligned* stream takes a reading at each clock-aligned boundary (multiples of ``interval`` seconds from
  midnight UTC) after the start (1.6 StopTxnAlignedData / ClockAlignedDataInterval, 2.0.1
  AlignedDataCtrlr.TxEndedInterval); the reading carries the boundary as its timestamp.
- ``interval`` is read on every check, so a configuration change applies at once; a changed interval restarts the
  count from the current position (no reading for the moment of the change). ``0`` = no readings.
- The caller checks once per tick: at most one reading per stream and tick, so ``tick_s`` bounds the resolution
  (an interval shorter than the tick, or a late tick, yields fewer readings; missed slots are not filled in). A
  reading is stamped with its interval boundary (start + n x interval, or the clock-aligned boundary); its values
  are those at the tick that took it.
- Memory is capped: each stream keeps at most ``max_readings`` readings, the oldest is dropped first (simulator
  option ``meter.tx_data_max_samples``); :attr:`TxDataBuffer.evicted` turns true on the first drop so the
  caller can warn once. The begin reading is kept apart and never dropped.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class TxReading:
    timestamp: datetime
    samples: Sequence[Any]
    context: str


@dataclass
class _Stream:
    interval: Optional[int] = None  # None = not checked yet (counting starts at the start of the transaction)
    last_index: Any = None


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc) if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _slot(ts: datetime, interval: int) -> Tuple[Tuple[int, int], datetime]:
    """Clock-aligned slot of ``ts`` (``(day, slot of the day)``) and the boundary that starts it."""
    u = _utc(ts)
    midnight = u.replace(hour=0, minute=0, second=0, microsecond=0)
    slot = int((u - midnight).total_seconds() // interval)
    return (u.toordinal(), slot), midnight + timedelta(seconds=slot * interval)


@dataclass
class TxDataBuffer:
    started_mono: float
    started_at: datetime
    max_readings: int
    begin: Optional[TxReading] = None
    evicted: bool = False  # a reading was dropped by the cap
    sampled: Deque[TxReading] = field(init=False)
    aligned: Deque[TxReading] = field(init=False)
    _sampled: _Stream = field(default_factory=_Stream)
    _aligned: _Stream = field(default_factory=_Stream)

    def __post_init__(self) -> None:
        self.sampled = deque(maxlen=max(1, self.max_readings))
        self.aligned = deque(maxlen=max(1, self.max_readings))

    @staticmethod
    def _due(stream: _Stream, interval: int, index: Any, start_index: Any) -> bool:
        if stream.interval is None:  # first check: count from the start of the transaction
            stream.interval, stream.last_index = interval, start_index
        elif interval != stream.interval:  # changed (or re-enabled): count from now
            stream.interval, stream.last_index = interval, index
            return False
        if interval <= 0:
            return False
        if index > stream.last_index:
            stream.last_index = index
            return True
        return False

    def sampled_due(self, now_mono: float, interval: int) -> Optional[datetime]:
        """The boundary (start + n x ``interval``) just passed, once per boundary since the start, else ``None``."""
        index = int((now_mono - self.started_mono) // interval) if interval > 0 else 0
        if not self._due(self._sampled, interval, index, 0):
            return None
        return self.started_at + timedelta(seconds=index * interval)

    def aligned_due(self, now: datetime, interval: int) -> Optional[datetime]:
        """The clock-aligned boundary just passed (once per boundary after the start), else ``None``."""
        if interval <= 0:
            # Record "disabled": a later non-zero interval counts from that moment, not from the start.
            self._due(self._aligned, interval, None, None)
            return None
        index, boundary = _slot(now, interval)
        start_index, _ = _slot(self.started_at, interval)
        return boundary if self._due(self._aligned, interval, index, start_index) else None

    def _add(self, q: Deque[TxReading], reading: TxReading) -> None:
        if reading.samples:
            if len(q) == q.maxlen:
                self.evicted = True
            q.append(reading)

    def add_sampled(self, reading: TxReading) -> None:
        self._add(self.sampled, reading)

    def add_aligned(self, reading: TxReading) -> None:
        self._add(self.aligned, reading)

    def all(self, end: Optional[TxReading] = None) -> List[TxReading]:
        """Begin reading, interval readings in time order, then ``end`` (empty readings left out)."""
        out = [self.begin] if self.begin is not None and self.begin.samples else []
        out += sorted([*self.sampled, *self.aligned], key=lambda r: r.timestamp)
        if end is not None and end.samples:
            out.append(end)
        return out


def union(*lists: Sequence[str]) -> Tuple[str, ...]:
    """Measurand lists merged, first occurrence kept (2.0.1 Sampled ∪ Aligned TxEndedMeasurands)."""
    seen: dict = {}
    for names in lists:
        for n in names:
            seen.setdefault(n, None)
    return tuple(seen)
