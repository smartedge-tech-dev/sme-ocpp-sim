"""RpcConnection against an in-memory websocket: correlation, timeouts, serialization, CALLERROR."""

import asyncio
import itertools
import json

import pytest
from rpc_fakes import FakeWs

from sme_ocpp_sim.rpc import OCPP201_ERRORS, CallError, FrameInfo, RpcConnection, after_this_reply


def _rpc(ws, handlers=None, **kw):
    ids = itertools.count(1)
    frames: list[FrameInfo] = []
    rpc = RpcConnection(ws, handlers, uid_factory=lambda: f"id{next(ids)}",
                        on_frame=frames.append, **kw)
    return rpc, frames


async def _until(cond, timeout=2.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


def run(coro_fn):
    """Run ``coro_fn(ws)`` with a receive loop going; close it afterwards."""
    async def main():
        ws = FakeWs()
        return await coro_fn(ws)
    return asyncio.run(main())


def test_call_returns_result_and_logs_action_on_response():
    async def scenario(ws):
        rpc, frames = _rpc(ws)
        ws.on_send = lambda raw: ws.feed([3, json.loads(raw)[1], {"currentTime": "t"}])
        rx = asyncio.create_task(rpc.run())
        assert await rpc.call("Heartbeat", {}) == {"currentTime": "t"}
        await rpc.close()
        await rx
        assert [(f.direction, f.message_type, f.uid, f.action) for f in frames] == [
            ("tx", 2, "id1", "Heartbeat"), ("rx", 3, "id1", "Heartbeat")]
    run(scenario)


def test_callerror_raises_with_code_and_details():
    async def scenario(ws):
        rpc, _ = _rpc(ws)
        ws.on_send = lambda raw: ws.feed([4, json.loads(raw)[1], "SecurityError", "no", {"a": 1}])
        rx = asyncio.create_task(rpc.run())
        with pytest.raises(CallError) as ei:
            await rpc.call("Authorize", {"idTag": "X"})
        assert (ei.value.code, ei.value.description, ei.value.details) == ("SecurityError", "no", {"a": 1})
        await rpc.close()
        await rx
    run(scenario)


def test_malformed_callresult_fails_the_pending_call():
    async def scenario(ws):
        rpc, _ = _rpc(ws)
        ws.on_send = lambda raw: ws.feed([3, json.loads(raw)[1], ["not", "an", "object"]])
        rx = asyncio.create_task(rpc.run())
        with pytest.raises(CallError) as ei:
            await rpc.call("Heartbeat", {})
        assert ei.value.code == "FormationViolation"
        await rpc.close()
        await rx
    run(scenario)


def test_timeout_then_late_response_is_dropped_and_next_call_works():
    async def scenario(ws):
        rpc, frames = _rpc(ws)
        rx = asyncio.create_task(rpc.run())
        with pytest.raises(asyncio.TimeoutError):
            await rpc.call("Heartbeat", {}, timeout=0.05)
        ws.feed([3, "id1", {"currentTime": "late"}])           # late answer to id1
        ws.feed([3, "nobody-asked", {}])                       # unsolicited
        ws.on_send = lambda raw: ws.feed([3, json.loads(raw)[1], {"currentTime": "t2"}])
        assert await rpc.call("Heartbeat", {}) == {"currentTime": "t2"}
        dropped = [f for f in frames if f.note]
        assert [(f.uid, f.note) for f in dropped] == [
            ("id1", "late or unsolicited response dropped"),
            ("nobody-asked", "late or unsolicited response dropped")]
        await rpc.close()
        await rx
    run(scenario)


def test_outbound_calls_are_serialized():
    async def scenario(ws):
        rpc, _ = _rpc(ws)
        rx = asyncio.create_task(rpc.run())
        a = asyncio.create_task(rpc.call("Heartbeat", {}))
        b = asyncio.create_task(rpc.call("StatusNotification", {}))
        await _until(lambda: len(ws.sent) >= 1)
        await asyncio.sleep(0.05)
        assert len(ws.sent) == 1                               # b waits for a's answer
        ws.feed([3, "id1", {}])
        await _until(lambda: len(ws.sent) == 2)
        assert ws.sent_frames()[1][2] == "StatusNotification"
        ws.feed([3, "id2", {}])
        assert await a == {} and await b == {}
        await rpc.close()
        await rx
    run(scenario)


def test_inbound_call_while_own_call_is_pending():
    """SteVe sends GetConfiguration before it answers BootNotification."""
    async def scenario(ws):
        async def get_configuration(payload):
            return {"configurationKey": []}
        rpc, _ = _rpc(ws, {"GetConfiguration": get_configuration})
        rx = asyncio.create_task(rpc.run())
        boot = asyncio.create_task(rpc.call("BootNotification", {}))
        await _until(lambda: len(ws.sent) == 1)
        ws.feed([2, "csms-1", "GetConfiguration", {"key": []}])
        await _until(lambda: len(ws.sent) == 2)
        assert ws.sent_frames()[1] == [3, "csms-1", {"configurationKey": []}]
        ws.feed([3, "id1", {"status": "Accepted"}])
        assert (await boot)["status"] == "Accepted"
        await rpc.close()
        await rx
    run(scenario)


def test_handler_may_send_its_own_call_without_deadlock():
    async def scenario(ws):
        rpc = None

        async def trigger(payload):
            await rpc.call("Heartbeat", {})                    # answered by the peer below
            return {"status": "Accepted"}
        rpc, _ = _rpc(ws, {"TriggerMessage": trigger})

        def peer(raw):
            msg = json.loads(raw)
            if msg[0] == 2:
                ws.feed([3, msg[1], {"currentTime": "t"}])
        ws.on_send = peer
        rx = asyncio.create_task(rpc.run())
        ws.feed([2, "csms-1", "TriggerMessage", {"requestedMessage": "Heartbeat"}])
        await _until(lambda: [3, "csms-1", {"status": "Accepted"}] in ws.sent_frames())
        await rpc.close()
        await rx
    run(scenario)


def test_inbound_errors_get_standard_callerror_codes():
    async def scenario(ws):
        async def reject(payload):
            raise CallError("PropertyConstraintViolation", "bad connectorId", {"field": "connectorId"})

        async def crash(payload):
            raise RuntimeError("boom")
        rpc, frames = _rpc(ws, {"Reset": reject, "UnlockConnector": crash})
        rx = asyncio.create_task(rpc.run())
        ws.feed([2, "u1", "FooBar", {}])
        ws.feed([2, "u2", "Reset", {"type": "Hard"}])
        ws.feed([2, "u3", "UnlockConnector", {"connectorId": 1}])
        ws.feed([2, "u4", "Heartbeat", "not-an-object"])
        ws.feed([2, "u5", "Heartbeat"])
        ws.feed([7, "u6", {}])
        ws.feed("garbage")                                     # no uniqueId: dropped, no answer
        await _until(lambda: len(ws.sent) == 6)
        await asyncio.sleep(0.05)
        codes = {f[1]: f[2] for f in ws.sent_frames()}
        assert codes == {"u1": "NotImplemented", "u2": "PropertyConstraintViolation",
                         "u3": "InternalError", "u4": "FormationViolation",
                         "u5": "FormationViolation", "u6": "ProtocolError"}
        assert [f for f in ws.sent_frames() if f[1] == "u2"][0][4] == {"field": "connectorId"}
        assert any(f.raw == "garbage" and f.note.startswith("dropped") for f in frames)
        await rpc.close()
        await rx
    run(scenario)


def test_error_codes_follow_the_version_table():
    async def scenario(ws):
        rpc, _ = _rpc(ws, error_codes=OCPP201_ERRORS)
        rx = asyncio.create_task(rpc.run())
        ws.feed([2, "u1", "Heartbeat", "not-an-object"])
        await _until(lambda: len(ws.sent) == 1)
        assert ws.sent_frames()[0][2] == "FormatViolation"
        await rpc.close()
        await rx
    run(scenario)


def test_close_fails_pending_call_and_rejects_new_calls():
    async def scenario(ws):
        rpc, _ = _rpc(ws)
        rx = asyncio.create_task(rpc.run())
        pending = asyncio.create_task(rpc.call("Heartbeat", {}))
        await _until(lambda: len(ws.sent) == 1)
        await rpc.close()
        await rx
        with pytest.raises(ConnectionError):
            await pending
        with pytest.raises(ConnectionError):
            await rpc.call("Heartbeat", {})
        assert rpc.closed.is_set()
    run(scenario)


def test_broken_frame_callback_does_not_break_rpc():
    async def scenario(ws):
        def boom(info):
            raise ValueError("observer bug")
        rpc = RpcConnection(ws, on_frame=boom)
        ws.on_send = lambda raw: ws.feed([3, json.loads(raw)[1], {}])
        rx = asyncio.create_task(rpc.run())
        assert await rpc.call("Heartbeat", {}) == {}
        await rpc.close()
        await rx
    run(scenario)


def test_after_this_reply_runs_once_the_answer_was_sent():
    class SlowWs(FakeWs):
        async def send(self, raw):
            await asyncio.sleep(0.1)  # the answer is slow to reach the socket
            await super().send(raw)

    async def scenario(ws):
        seen = []

        async def reset(payload):
            assert after_this_reply(lambda: seen.append(list(ws.sent_frames())))
            return {"status": "Accepted"}
        rpc, _ = _rpc(ws, {"Reset": reset})
        rx = asyncio.create_task(rpc.run())
        ws.feed([2, "csms-1", "Reset", {"type": "Immediate"}])
        await _until(lambda: seen)
        assert seen == [[[3, "csms-1", {"status": "Accepted"}]]]  # the hook saw the answer already sent
        assert after_this_reply(lambda: None) is False          # outside a handler: not registered
        await rpc.close()
        await rx
    run(lambda ws: scenario(SlowWs()))
