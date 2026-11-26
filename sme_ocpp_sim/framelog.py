"""Frame log: a bounded ring buffer of OCPP-J frames with a global sequence number.

Fed through the RPC layer's ``on_frame`` callback (``rpc`` never imports this module).
Clients poll with ``since(seq)`` and de-duplicate by ``seq``; ``to_jsonl()`` exports.
"""

from __future__ import annotations

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
        out = [e for e in self._buf if e.seq > seq
               and (charger is None or e.charger == charger)
               and (action is None or e.action == action)
               and (direction is None or e.direction == direction)]
        return out[-limit:] if limit else out

    def clear(self) -> None:
        self._buf.clear()

    def to_jsonl(self, entries: Optional[Iterable[FrameEntry]] = None) -> str:
        rows = self._buf if entries is None else entries
        return "".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in rows)
