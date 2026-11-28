"""Frame rewriting for a trace replay (v0.3 plan §3.7): the version-independent part.

A replay re-sends recorded payloads, which must not look stale or collide with the original run. The
:class:`Rewriter` here shifts every ISO 8601 date-time string of a frame (any depth, also inside DataTransfer
data) by one offset computed when the frame is sent: the earliest timestamp of that frame lands on "now", the
spacing inside the frame is kept. The version rules live in :mod:`.rewrite_v16` (1.6
``transactionId`` re-mapping) and :mod:`.rewrite_v201` (2.0.1 / 2.1 ``transactionId`` run suffix).

Rewriters work on copies; the trace is never changed. ``uniqueId`` values are not a rewriter's job: the RPC layer
draws a new one for every CALL.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

#: An ISO 8601 date-time as OCPP sends it (``2026-01-01T00:00:00Z``, ``...00.123+09:00``, naive also accepted).
ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?")


def parse_datetime(text: Any) -> Optional[datetime]:
    if not isinstance(text, str) or not ISO_DATETIME.fullmatch(text):
        return None
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None


def shift_datetime(text: str, shift: timedelta) -> str:
    """``text`` moved by ``shift``, in the same style (``Z`` stays ``Z``, an offset stays, fraction kept)."""
    dt = parse_datetime(text)
    if dt is None:
        return text
    m = ISO_DATETIME.fullmatch(text)
    out = (dt + shift).isoformat(timespec="milliseconds" if m and m.group(2) else "seconds")
    if text.endswith("Z") and out.endswith("+00:00"):
        out = out[:-6] + "Z"
    return out


def walk(obj: Any, fn) -> Any:
    """``obj`` with every ``(key, value)`` leaf of dicts replaced by ``fn(key, value)`` (lists walked too)."""
    if isinstance(obj, dict):
        return {k: (walk(v, fn) if isinstance(v, (dict, list)) else fn(k, v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk(v, fn) if isinstance(v, (dict, list)) else fn(None, v) for v in obj]
    return obj


def earliest_datetime(payloads: Iterable[Any]) -> Optional[datetime]:
    """The earliest timezone-aware date-time string in ``payloads`` (``None`` when there is none)."""
    found: List[datetime] = []

    def see(_k, v):
        dt = parse_datetime(v)
        if dt is not None and dt.tzinfo is not None:
            found.append(dt)
        return v

    for p in payloads:
        walk(p, see)
    return min(found) if found else None


class Rewriter:
    """Timestamp shift; subclasses add the version rules. ``notes`` collects what could not be rewritten."""

    version = "?"

    def __init__(self, *, shift: Optional[timedelta] = None, run_id: str = "", auto_shift: bool = False) -> None:
        #: A fixed offset, or (``auto_shift``) one computed per frame at the moment it is sent.
        self.shift = shift
        self.auto_shift = auto_shift
        self.run_id = run_id
        self.notes: List[str] = []
        self._last_shift: Optional[timedelta] = None

    def _frame_shift(self, payload: Any) -> Optional[timedelta]:
        """``auto_shift``: the earliest aware date-time of THIS frame becomes "now" (offsets inside it are kept).
        A frame without one reuses the last offset."""
        if not self.auto_shift:
            return self.shift
        first = earliest_datetime([payload])
        if first is not None:
            self._last_shift = datetime.now(timezone.utc) - first
        return self._last_shift

    def _times(self, payload: Any) -> Any:
        shift = self._frame_shift(payload)
        if not shift:
            return copy.deepcopy(payload)
        return walk(payload, lambda _k, v: shift_datetime(v, shift) if isinstance(v, str) else v)

    def call(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """The payload of a recorded charger -> CSMS CALL as it is sent now."""
        return self._times(payload)

    def reply(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """The recorded answer to a CSMS -> charger CALL as it is sent now."""
        return self._times(payload)

    def learn(self, action: str, recorded: Optional[Dict[str, Any]], replayed: Optional[Dict[str, Any]]) -> None:
        """Called with the recorded and the new CALLRESULT payload of every CALL (either may be ``None``)."""
