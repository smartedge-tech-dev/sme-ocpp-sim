"""Frame log ring buffer: seq, filters, bound, JSONL."""

import json

from sme_ocpp_sim.framelog import FrameLog
from sme_ocpp_sim.rpc import FrameInfo


def test_seq_filters_bound_and_jsonl():
    log = FrameLog(maxlen=3, clock=lambda: 1.5)
    log.add("A", FrameInfo("tx", '[2,"1","Heartbeat",{}]', 2, "1", "Heartbeat"))
    log.add("B", FrameInfo("rx", '[3,"1",{}]', 3, "1", "Heartbeat"))
    cb = log.callback("A")
    cb(FrameInfo("rx", "garbage", None, None, None, "dropped: frame is not valid JSON"))
    log.add("A", FrameInfo("tx", '[2,"2","StatusNotification",{}]', 2, "2", "StatusNotification"))

    assert log.last_seq == 4
    assert [e.seq for e in log.since(0)] == [2, 3, 4]          # oldest fell out
    assert [e.seq for e in log.since(2)] == [3, 4]
    assert [e.seq for e in log.since(0, charger="A")] == [3, 4]
    assert [e.seq for e in log.since(0, action="Heartbeat")] == [2]
    assert [e.seq for e in log.since(0, direction="tx")] == [4]
    assert [e.seq for e in log.since(0, limit=1)] == [4]
    e3 = log.since(2)[0]
    assert e3.message_type is None and e3.note.startswith("dropped")

    rows = [json.loads(line) for line in log.to_jsonl().splitlines()]
    assert rows[0] == {"seq": 2, "ts": 1.5, "charger": "B", "direction": "rx",
                       "message_type": "CALLRESULT", "uid": "1", "action": "Heartbeat",
                       "raw": '[3,"1",{}]', "note": ""}
    log.clear()
    assert log.since(0) == [] and log.last_seq == 4
