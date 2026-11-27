import importlib.util

import pytest

from sme_ocpp_sim.protocol import registry


def test_versions_listed():
    vs = {v.version: v for v in registry.list_versions()}
    assert vs["1.6"].available
    assert vs["2.0.1"].available
    assert not vs["2.1"].available and vs["2.1"].note == "coming in v0.3"


def test_check_version():
    assert registry.check_version("1.6") == "1.6"
    assert registry.check_version("2.0.1") == "2.0.1"
    with pytest.raises(ValueError, match="coming in v0.3"):
        registry.check_version("2.1")
    with pytest.raises(ValueError, match="unknown OCPP version"):
        registry.check_version("1.5")


@pytest.mark.parametrize("version,msg", [("2.1", "coming in v0.3")])
def test_get_adapter_class_unavailable(version, msg):
    with pytest.raises(ValueError, match=msg):
        registry.get_adapter_class(version)


@pytest.mark.skipif(importlib.util.find_spec("sme_ocpp_sim.protocol.v16.adapter") is None,
                    reason="Adapter16 lands in M3 part 2")
def test_get_adapter_class_16():
    from sme_ocpp_sim.protocol.base import VersionAdapter

    cls = registry.get_adapter_class("1.6")
    assert issubclass(cls, VersionAdapter) and cls.version == "1.6"


def test_get_classes_201():
    from sme_ocpp_sim.protocol.base import VersionAdapter
    from sme_ocpp_sim.protocol.station import VersionStation

    adapter, station = registry.get_adapter_class("2.0.1"), registry.get_station_class("2.0.1")
    assert issubclass(adapter, VersionAdapter) and adapter.version == "2.0.1" and adapter.subprotocol == "ocpp2.0.1"
    assert issubclass(station, VersionStation) and station.__module__.startswith("sme_ocpp_sim.protocol.v201")
