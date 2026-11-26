"""OCPP-J frame encoding and decoding (version neutral).

    CALL       [2, uniqueId, action, payload]
    CALLRESULT [3, uniqueId, payload]
    CALLERROR  [4, uniqueId, errorCode, errorDescription, errorDetails]

Decoding never raises for bad input other than :class:`BadFrame`, which tells the
caller whether a uniqueId could be recovered (and so whether a CALLERROR can be sent).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Union

CALL, CALLRESULT, CALLERROR = 2, 3, 4


@dataclass(frozen=True)
class Call:
    uid: str
    action: str
    payload: dict


@dataclass(frozen=True)
class CallResult:
    uid: str
    payload: dict


@dataclass(frozen=True)
class CallErrorFrame:
    uid: str
    code: str
    description: str = ""
    details: dict = field(default_factory=dict)


Frame = Union[Call, CallResult, CallErrorFrame]

# BadFrame kinds
NO_UID = "no_uid"              # not JSON / not an array / no string uniqueId: cannot answer
BAD_CALL = "bad_call"          # CALL with wrong shape or non-object payload
BAD_RESULT = "bad_result"      # CALLRESULT / CALLERROR with wrong shape
UNKNOWN_TYPE = "unknown_type"  # MessageTypeId other than 2 / 3 / 4


class BadFrame(ValueError):
    def __init__(self, kind: str, reason: str, uid: Optional[str] = None,
                 message_type: Any = None) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.uid = uid
        self.message_type = message_type


def _dumps(frame: list) -> str:
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)


def encode_call(uid: str, action: str, payload: dict) -> str:
    return _dumps([CALL, uid, action, payload])


def encode_result(uid: str, payload: dict) -> str:
    return _dumps([CALLRESULT, uid, payload])


def encode_error(uid: str, code: str, description: str = "", details: Optional[dict] = None) -> str:
    return _dumps([CALLERROR, uid, code, description, details or {}])


def decode(raw: str) -> Frame:
    try:
        msg = json.loads(raw)
    except ValueError:
        raise BadFrame(NO_UID, "frame is not valid JSON") from None
    if not isinstance(msg, list) or len(msg) < 2:
        raise BadFrame(NO_UID, "frame is not a JSON array with a uniqueId")
    mtype, uid = msg[0], msg[1]
    if not isinstance(uid, str):
        raise BadFrame(NO_UID, "uniqueId is not a string", message_type=mtype)
    # bool is an int subclass: reject [true, ...] explicitly.
    if isinstance(mtype, bool) or mtype not in (CALL, CALLRESULT, CALLERROR):
        raise BadFrame(UNKNOWN_TYPE, f"unknown MessageTypeId {mtype!r}", uid, mtype)

    if mtype == CALL:
        if len(msg) != 4 or not isinstance(msg[2], str) or not isinstance(msg[3], dict):
            raise BadFrame(BAD_CALL, "CALL frame or payload is malformed", uid, mtype)
        return Call(uid, msg[2], msg[3])
    if mtype == CALLRESULT:
        if len(msg) != 3 or not isinstance(msg[2], dict):
            raise BadFrame(BAD_RESULT, "CALLRESULT frame or payload is malformed", uid, mtype)
        return CallResult(uid, msg[2])
    if len(msg) != 5 or not isinstance(msg[2], str):
        raise BadFrame(BAD_RESULT, "CALLERROR frame is malformed", uid, mtype)
    desc = msg[3] if isinstance(msg[3], str) else str(msg[3])
    details = msg[4] if isinstance(msg[4], dict) else {}
    return CallErrorFrame(uid, msg[2], desc, details)
