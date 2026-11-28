"""Action layer (v0.3 M0, ``scenario/ops.py``): the errors it reports without HTTP. The behaviour of each action
through its route is covered by test_web.py (the routes call these functions)."""

from __future__ import annotations

import asyncio

import pytest

from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.scenario import ops


def _engine() -> OcppEngine:
    return OcppEngine(SimConfig.model_validate({"csms_defaults": {"url": "ws://127.0.0.1:9/ocpp"},
                                                "chargers": [{"id": "CP1"}]}))


def _kind(aw) -> str:
    with pytest.raises(ops.OpError) as e:
        asyncio.run(aw)
    return e.value.kind


def test_error_kinds_without_http():
    engine = _engine()
    assert _kind(ops.plug(engine, "nope")) == "not_found"
    assert _kind(ops.plug(engine, "CP1", 99)) == "invalid"
    assert _kind(ops.swipe(engine, "CP1", 1, None)) == "invalid"
    assert _kind(ops.fault(engine, "CP1", 1, "NoSuchCode")) == "invalid"
    assert _kind(ops.connect(engine, "CP1")) == "invalid"        # engine not started
    assert _kind(ops.connect(engine, "nope")) == "not_found"
    assert _kind(ops.send(engine, "CP1", raw="[2]", payload={})) == "invalid"
    assert _kind(ops.send(engine, "CP1", raw="[2]", payload=None)) == "invalid"  # an explicit None is given
    assert _kind(ops.send(engine, "CP1")) == "invalid"
    assert _kind(ops.send(engine, "CP1", action="Heartbeat", payload="x" * (ops.MAX_INJECT_BYTES + 1))) == "too_large"
    assert _kind(ops.impair(engine, "CP1", {"no_such_field": 1})) == "invalid"
    with pytest.raises(ops.OpError) as e:
        ops.drop(engine, "nope")
    assert e.value.kind == "not_found" and "nope" in str(e.value)


def test_http_status_covers_every_kind():
    assert set(ops.HTTP_STATUS) == {"invalid", "not_found", "conflict", "too_large", "timeout"}
