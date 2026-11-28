"""charging.tx_data and the per-phase helpers of charging.power (v0.2.2 B)."""

from datetime import datetime, timedelta, timezone

from sme_ocpp_sim.charging.power import AcSupply, phase_currents, phase_voltages
from sme_ocpp_sim.charging.tx_data import TxDataBuffer, TxReading, union

T0 = datetime(2026, 11, 28, 10, 0, 7, tzinfo=timezone.utc)


def test_phase_voltages_modes():
    one, three = AcSupply(230.0, 1), AcSupply(230.0, 3)
    assert phase_voltages(three, "off") == []
    assert phase_voltages(one, "L-N") == [("L1-N", 230.0)]
    assert phase_voltages(three, "L-N") == [("L1-N", 230.0), ("L2-N", 230.0), ("L3-N", 230.0)]
    assert phase_voltages(three, "L-L") == [("L1-L2", 398.4), ("L2-L3", 398.4), ("L3-L1", 398.4)]
    assert phase_voltages(one, "L-L") == [("L1-N", 230.0)]  # no line-to-line pair on one phase
    delta = AcSupply(200.0, 3, line_to_line=True)  # voltage given line-to-line
    assert phase_voltages(delta, "L-L") == [("L1-L2", 200.0), ("L2-L3", 200.0), ("L3-L1", 200.0)]
    assert phase_voltages(delta, "L-N")[0] == ("L1-N", 115.5)


def test_phase_currents_all_supply_phases():
    three = AcSupply(230.0, 3)
    assert phase_currents(10.0, three, 3) == [("L1", 10.0), ("L2", 10.0), ("L3", 10.0)]
    assert phase_currents(16.0, three, 1) == [
        ("L1", 16.0), ("L2", 0.0), ("L3", 0.0)]


def test_sampled_stream_counts_from_the_start_and_restarts_on_change():
    b = TxDataBuffer(100.0, T0, 10)
    assert not b.sampled_due(100.5, 2)
    assert b.sampled_due(102.1, 2)
    assert not b.sampled_due(103.0, 2)
    assert b.sampled_due(106.5, 2)  # several intervals late: one reading
    assert not b.sampled_due(107.0, 5)  # changed: counted from now
    assert b.sampled_due(110.1, 5)
    assert not b.sampled_due(200.0, 0)  # 0 = no readings
    assert not b.sampled_due(201.0, 1)  # re-enabled: counted from now
    assert b.sampled_due(202.0, 1)


def test_aligned_stream_boundaries_after_the_start():
    b = TxDataBuffer(0.0, T0, 10)
    assert b.aligned_due(T0 + timedelta(seconds=1), 15) is None  # 10:00:08, slot of the start (10:00:00)
    assert b.aligned_due(T0 + timedelta(seconds=9), 15) == T0.replace(second=15)
    assert b.aligned_due(T0 + timedelta(seconds=10), 15) is None
    assert b.aligned_due(T0 + timedelta(seconds=40), 15) == T0.replace(second=45)
    assert b.aligned_due(T0, 0) is None


def test_cap_keeps_begin_and_newest_readings_in_order():
    b = TxDataBuffer(0.0, T0, 2, begin=TxReading(T0, ["b"], "Transaction.Begin"))
    b.add_sampled(TxReading(T0 + timedelta(seconds=1), [0], "Sample.Periodic"))
    b.add_sampled(TxReading(T0 + timedelta(seconds=2), [1], "Sample.Periodic"))
    assert not b.evicted
    for i in (2, 3):
        b.add_sampled(TxReading(T0 + timedelta(seconds=i + 1), [i], "Sample.Periodic"))
    assert b.evicted
    b.add_aligned(TxReading(T0 + timedelta(seconds=2.5), ["c"], "Sample.Clock"))  # own cap: not evicted by sampled
    b.add_aligned(TxReading(T0, [], "Sample.Clock"))  # empty readings are not kept
    out = b.all(TxReading(T0 + timedelta(seconds=9), ["e"], "Transaction.End"))
    assert [r.samples for r in out] == [["b"], ["c"], [2], [3], ["e"]]


def test_sampled_readings_are_stamped_at_their_boundary():
    b = TxDataBuffer(100.0, T0, 10)
    b.sampled_due(100.0, 2)
    assert b.sampled_due(104.7, 2) == T0 + timedelta(seconds=4)
    assert union(["a", "b"], ["b", "c"]) == ("a", "b", "c")
