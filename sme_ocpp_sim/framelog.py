"""Frame log: a bounded ring buffer of OCPP-J frames with a global sequence number.

Fed through the RPC layer's ``on_frame`` callback (``rpc`` never imports this module).
Clients poll with ``since(seq)`` and de-duplicate by ``seq``; ``to_jsonl()`` exports.

Secrets (#54): :func:`mask_payload` / :func:`mask_frame` replace the secret values a CSMS sends to a station with
:data:`SECRET_MASK` - 1.6 ChangeConfiguration ``AuthorizationKey`` (``value``), 2.0.1 SetVariables
``BasicAuthPassword`` (``attributeValue``), SetNetworkProfile's VPN / APN credentials, and where such a value can
come back: the 2.0.1 / 2.1 NotifyReport request (``variableAttribute[].value``) and the answers to GetVariables
(``attributeValue``) and the 1.6 GetConfiguration (``AuthorizationKey`` ``value``; :data:`RESULT_SECRET_ACTIONS`:
a CALLRESULT carries the action of its CALL, see ``rpc.FrameInfo``). :meth:`FrameLog.add`
masks the text it stores, so both the charger simulator's frame log and the Mini CSMS's are covered; the frame on
the wire is never changed (``FrameInfo.raw`` is only read). A certificate (CertificateSigned, InstallCertificate) is
not a secret and is kept.
"""

from __future__ import annotations

import copy
import itertools
import json
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Optional

MESSAGE_TYPE_NAMES = {2: "CALL", 3: "CALLRESULT", 4: "CALLERROR"}

#: What a masked secret value is replaced with (the same text as ``config.PASSWORD_MASK``).
SECRET_MASK = "********"
#: CALL actions whose request payload can carry a secret.
SECRET_ACTIONS = frozenset({"ChangeConfiguration", "SetVariables", "SetNetworkProfile", "NotifyReport"})
#: CALL actions whose CALLRESULT can carry a secret.
RESULT_SECRET_ACTIONS = frozenset({"GetVariables", "GetConfiguration"})
_SECRET_KEYS_16 = frozenset({"AuthorizationKey"})
_SECRET_VARIABLES_201 = frozenset({"BasicAuthPassword"})
_SECRET_PROFILE_FIELDS = {"vpn": ("password", "key"), "apn": ("apnPassword", "simPin")}


def _mask_in_place(action: str, p: dict) -> bool:
    """Mask the secrets of one request payload ``p`` (mutated); True when something was masked."""
    hit = False
    if action == "ChangeConfiguration":
        if p.get("key") in _SECRET_KEYS_16 and "value" in p:
            p["value"], hit = SECRET_MASK, True
    elif action == "SetVariables":
        for d in p.get("setVariableData") or []:
            if (isinstance(d, dict) and isinstance(d.get("variable"), dict)
                    and d["variable"].get("name") in _SECRET_VARIABLES_201 and "attributeValue" in d):
                d["attributeValue"], hit = SECRET_MASK, True
    elif action == "NotifyReport":
        for d in p.get("reportData") or []:
            if isinstance(d, dict) and isinstance(d.get("variable"), dict)                     and d["variable"].get("name") in _SECRET_VARIABLES_201:
                for a in d.get("variableAttribute") or []:
                    if isinstance(a, dict) and "value" in a:
                        a["value"], hit = SECRET_MASK, True
    elif action == "SetNetworkProfile":
        data = p.get("connectionData")
        for part, fields in _SECRET_PROFILE_FIELDS.items():
            sub = data.get(part) if isinstance(data, dict) else None
            for f in fields if isinstance(sub, dict) else ():
                if f in sub:
                    sub[f], hit = SECRET_MASK, True
    return hit


def _mask_result_in_place(action: str, p: dict) -> bool:
    """Mask the secrets of one CALLRESULT payload ``p`` of a CALL ``action`` (mutated)."""
    hit = False
    if action == "GetVariables":
        for d in p.get("getVariableResult") or []:
            if (isinstance(d, dict) and isinstance(d.get("variable"), dict)
                    and d["variable"].get("name") in _SECRET_VARIABLES_201 and "attributeValue" in d):
                d["attributeValue"], hit = SECRET_MASK, True
    elif action == "GetConfiguration":
        for d in p.get("configurationKey") or []:
            if isinstance(d, dict) and d.get("key") in _SECRET_KEYS_16 and "value" in d:
                d["value"], hit = SECRET_MASK, True
    return hit


def mask_payload(action: Optional[str], payload: Any) -> Any:
    """``payload`` of a CALL ``action`` with its secret values masked: a masked deep copy, or ``payload`` itself
    when it carries no secret (never mutated)."""
    if action not in SECRET_ACTIONS or not isinstance(payload, dict):
        return payload
    out = copy.deepcopy(payload)
    return out if _mask_in_place(action, out) else payload


def mask_frame(raw: str, action: Optional[str] = None) -> str:
    """The OCPP-J frame text ``raw`` with the secrets of its CALL payload masked (re-serialised compactly only when
    something was masked; anything unparseable is returned as is). ``action`` (when known) skips the parse for
    every other action."""
    if action is not None and action not in SECRET_ACTIONS and action not in RESULT_SECRET_ACTIONS:
        return raw
    try:
        frame = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(frame, list) or not frame:
        return raw
    if frame[0] == 2 and len(frame) == 4 and frame[2] in SECRET_ACTIONS and isinstance(frame[3], dict):
        hit = _mask_in_place(frame[2], frame[3])
    elif frame[0] == 3 and len(frame) == 3 and action in RESULT_SECRET_ACTIONS and isinstance(frame[2], dict):
        hit = _mask_result_in_place(action, frame[2])   # the action comes from the CALL this answers
    else:
        hit = False
    if not hit:
        return raw
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


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
    def oldest_seq(self) -> int:
        """Seq of the oldest entry still in the buffer (``last_seq + 1`` when empty)."""
        return self._buf[0].seq if self._buf else self._seq + 1

    @property
    def maxlen(self) -> int:
        return self._buf.maxlen or 0

    def resize(self, maxlen: int) -> None:
        """Change the capacity, keeping the newest entries."""
        if maxlen != self._buf.maxlen:
            self._buf = deque(self._buf, maxlen=maxlen)

    def add(self, charger: str, info: Any) -> FrameEntry:
        """Record one frame (secrets masked, :func:`mask_frame`). ``info`` is an ``rpc.FrameInfo`` (duck-typed)."""
        self._seq += 1
        mtype = getattr(info, "message_type", None)
        action = getattr(info, "action", None)
        entry = FrameEntry(
            seq=self._seq, ts=self._clock(), charger=charger, direction=info.direction,
            message_type=MESSAGE_TYPE_NAMES.get(mtype) if mtype is not None else None,
            uid=getattr(info, "uid", None), action=action,
            raw=(mask_frame(info.raw, action) if mtype in (2, None) or (mtype == 3 and action in RESULT_SECRET_ACTIONS)
                 else info.raw), note=getattr(info, "note", ""),
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
