"""OCPP-J frame encoding / decoding."""

import json

import pytest

from sme_ocpp_sim.rpc import framer
from sme_ocpp_sim.rpc.framer import BadFrame, Call, CallErrorFrame, CallResult


def test_encode_is_compact_and_keeps_non_ascii():
    raw = framer.encode_call("u1", "DataTransfer", {"data": "充電"})
    assert raw == '[2,"u1","DataTransfer",{"data":"充電"}]'
    assert json.loads(framer.encode_result("u1", {})) == [3, "u1", {}]
    assert json.loads(framer.encode_error("u1", "NotImplemented", "x")) == [4, "u1", "NotImplemented", "x", {}]


@pytest.mark.parametrize("raw, frame", [
    ('[2,"a","Heartbeat",{}]', Call("a", "Heartbeat", {})),
    ('[3,"a",{"currentTime":"t"}]', CallResult("a", {"currentTime": "t"})),
    ('[4,"a","GenericError","d",{"k":1}]', CallErrorFrame("a", "GenericError", "d", {"k": 1})),
    ('[4,"a","GenericError",5,null]', CallErrorFrame("a", "GenericError", "5", {})),
])
def test_decode_valid(raw, frame):
    assert framer.decode(raw) == frame


@pytest.mark.parametrize("raw, kind, uid", [
    ("not json", framer.NO_UID, None),
    ('{"a":1}', framer.NO_UID, None),
    ("[2]", framer.NO_UID, None),
    ('[2,5,"Heartbeat",{}]', framer.NO_UID, None),
    ('[7,"x",{}]', framer.UNKNOWN_TYPE, "x"),
    ('[true,"x",{}]', framer.UNKNOWN_TYPE, "x"),
    ('["2","x","Heartbeat",{}]', framer.UNKNOWN_TYPE, "x"),
    ('[2,"x","Heartbeat"]', framer.BAD_CALL, "x"),
    ('[2,"x","Heartbeat","str"]', framer.BAD_CALL, "x"),
    ('[2,"x",1,{}]', framer.BAD_CALL, "x"),
    ('[3,"x",[]]', framer.BAD_RESULT, "x"),
    ('[3,"x",{},1]', framer.BAD_RESULT, "x"),
    ('[4,"x","Code"]', framer.BAD_RESULT, "x"),
    ('[4,"x",1,"d",{}]', framer.BAD_RESULT, "x"),
])
def test_decode_bad(raw, kind, uid):
    with pytest.raises(BadFrame) as ei:
        framer.decode(raw)
    assert ei.value.kind == kind and ei.value.uid == uid
