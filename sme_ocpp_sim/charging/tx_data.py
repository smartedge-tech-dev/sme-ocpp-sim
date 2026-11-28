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
- Memory is capped: at most ``max_readings`` interval readings are kept, the oldest is dropped first (simulator
  option ``meter.tx_data_max_samples``). The begin reading is kept apart and never dropped.
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
    readings: Deque[TxReading] = field(init=False)
    _sampled: _Stream = field(default_factory=_Stream)
    _aligned: _Stream = field(default_factory=_Stream)

    def __post_init__(self) -> None:
        self.readings = deque(maxlen=max(1, self.max_readings))

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

    def sampled_due(self, now_mono: float, interval: int) -> bool:
        """True once per elapsed ``interval`` since the start of the transaction."""
        index = int((now_mono - self.started_mono) // interval) if interval > 0 else 0
        return self._due(self._sampled, interval, index, 0)

    def aligned_due(self, now: datetime, interval: int) -> Optional[datetime]:
        """The clock-aligned boundary just passed (once per boundary after the start), else ``None``."""
        if interval <= 0:
            self._due(self._aligned, interval, None, None)
            return None
        index, boundary = _slot(now, interval)
        start_index, _ = _slot(self.started_at, interval)
        return boundary if self._due(self._aligned, interval, index, start_index) else None

    def add(self, reading: TxReading) -> None:
        if reading.samples:
            self.readings.append(reading)

    def all(self, end: Optional[TxReading] = None) -> List[TxReading]:
        """Begin reading, interval readings in time order, then ``end`` (empty readings left out)."""
        out = [self.begin] if self.begin is not None and self.begin.samples else []
        out += sorted(self.readings, key=lambda r: r.timestamp)
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
