"""Frame log: a bounded ring buffer of OCPP-J frames with a global sequence number.

Fed through the RPC layer's ``on_frame`` callback (``rpc`` never imports this module).
Clients poll with ``since(seq)`` and de-duplicate by ``seq``; ``to_jsonl()`` exports.
"""

from __future__ import annotations

import itertools
import json
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Optional

MESSAGE_TYPE_NAMES = {2: "CALL", 3: "CALLRESULT", 4: "CALLERROR"}


@dataclass(frozen=True)
class FrameEntry:
    seq: int
    ts: float                      # wall clock, seconds since the epoch
    charger: str
    direction: str                 # "rx" | "tx" (seen from the charger)
    message_type: Optional[str]    # "CALL" | "CALLRESULT" | "CALLERROR" | None (unparseable)
    uid: Optional[str]
    action: Optional[str]
    raw: str
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class FrameLog:
    def __init__(self, maxlen: int = 5000, clock: Callable[[], float] = time.time) -> None:
        self._buf: deque[FrameEntry] = deque(maxlen=maxlen)
        self._seq = 0
        self._clock = clock

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def maxlen(self) -> int:
        return self._buf.maxlen or 0

    def resize(self, maxlen: int) -> None:
        """Change the capacity, keeping the newest entries."""
        if maxlen != self._buf.maxlen:
            self._buf = deque(self._buf, maxlen=maxlen)

    def add(self, charger: str, info: Any) -> FrameEntry:
        """Record one frame. ``info`` is an ``rpc.FrameInfo`` (duck-typed)."""
        self._seq += 1
        mtype = getattr(info, "message_type", None)
        entry = FrameEntry(
            seq=self._seq, ts=self._clock(), charger=charger, direction=info.direction,
            message_type=MESSAGE_TYPE_NAMES.get(mtype) if mtype is not None else None,
            uid=getattr(info, "uid", None), action=getattr(info, "action", None),
            raw=info.raw, note=getattr(info, "note", ""),
        )
        self._buf.append(entry)
        return entry

    def callback(self, charger: str) -> Callable[[Any], None]:
        """An ``on_frame`` callback that records frames for ``charger``."""
        return lambda info: self.add(charger, info)

    def since(self, seq: int = 0, *, charger: Optional[str] = None, action: Optional[str] = None,
              direction: Optional[str] = None, limit: Optional[int] = None) -> list[FrameEntry]:
        """Entries after ``seq`` (oldest first) matching the filters; with ``limit`` only the newest ``limit``.

        Sequence numbers in the buffer are contiguous, so only the ``last_seq - seq`` newest entries are scanned
        (from the right), not the whole buffer."""
        out = []
        for e in itertools.islice(reversed(self._buf), max(0, self._seq - seq)):
            if ((charger is None or e.charger == charger) and (action is None or e.action == action)
                    and (direction is None or e.direction == direction)):
                out.append(e)
                if limit and len(out) >= limit:
                    break
        out.reverse()
        return out

    def clear(self) -> None:
        self._buf.clear()

    def to_jsonl(self, entries: Optional[Iterable[FrameEntry]] = None) -> str:
        rows = self._buf if entries is None else entries
        return "".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in rows)
