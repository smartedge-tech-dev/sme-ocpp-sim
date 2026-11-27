import pytest

from sme_ocpp_sim.protocol.base import ConfigKeyDef, ConfigStatus, KeyType
from sme_ocpp_sim.protocol.v16.config_keys import KEYS, KEYS_BY_NAME
from sme_ocpp_sim.protocol.v16.config_store import ConfigStore, normalize

# ------------------------------------------------------------------ table sanity


def test_names_unique_and_known_to_ocpp_library():
    assert len(KEYS_BY_NAME) == len(KEYS)
    ocpp_enums = pytest.importorskip("ocpp.v16.enums")
    lib = {k.value for k in ocpp_enums.ConfigurationKey}
    assert set(KEYS_BY_NAME) <= lib


def test_defaults_fit_their_types():
    for k in KEYS:
        assert normalize(k, k.default) == k.default, k.name


def test_runtime_keys_present():
    for name in ("HeartbeatInterval", "MeterValueSampleInterval", "ClockAlignedDataInterval",
                 "MeterValuesSampledData", "StopTxnSampledData", "NumberOfConnectors", "AuthorizeRemoteTxRequests",
                 "LocalPreAuthorize", "ConnectionTimeOut", "ChargeProfileMaxStackLevel",
                 "ChargingScheduleAllowedChargingRateUnit", "ChargingScheduleMaxPeriods",
                 "MaxChargingProfilesInstalled", "ResetRetries", "WebSocketPingInterval", "SupportedFeatureProfiles"):
        assert name in KEYS_BY_NAME


def test_vendor_doc_readonly_flags():
    # WY-L22-0061A p.9: R keys
    for name in ("GetConfigurationMaxKeys", "NumberOfConnectors", "SupportedFeatureProfiles",
                 "ChargeProfileMaxStackLevel", "ChargingScheduleAllowedChargingRateUnit",
                 "ChargingScheduleMaxPeriods", "MaxChargingProfilesInstalled"):
        assert KEYS_BY_NAME[name].readonly and KEYS_BY_NAME[name].verified
    assert not KEYS_BY_NAME["HeartbeatInterval"].readonly


# ------------------------------------------------------------------ ConfigStore


@pytest.fixture
def store():
    return ConfigStore(KEYS, {"HeartbeatInterval": "120", "NumberOfConnectors": "2"})


def test_get_all_and_selected(store):
    known, unknown = store.get()
    assert len(known) == len(KEYS) and unknown == []
    known, unknown = store.get(["HeartbeatInterval", "Nope"])
    assert [(e.key, e.readonly, e.value) for e in known] == [("HeartbeatInterval", False, "120")]
    assert unknown == ["Nope"]


def test_overrides_apply_and_bypass_readonly(store):
    assert store.get_int("NumberOfConnectors") == 2


def test_bad_overrides_raise():
    with pytest.raises(ValueError, match="unknown configuration key"):
        ConfigStore(KEYS, {"Nope": "1"})
    with pytest.raises(ValueError, match="not a valid int"):
        ConfigStore(KEYS, {"HeartbeatInterval": "abc"})


def test_set_results(store):
    assert store.set("Nope", "1") == ConfigStatus.NOT_SUPPORTED == "NotSupported"
    assert store.set("NumberOfConnectors", "3") == "Rejected"
    assert store.set("HeartbeatInterval", "x") == "Rejected"
    assert store.set("HeartbeatInterval", "-1") == "Rejected"
    assert store.set("LocalPreAuthorize", "maybe") == "Rejected"
    assert store.set("MeterValuesSampledData", "Energy.Active.Import.Register,Bogus") == "Rejected"
    assert store.set("HeartbeatInterval", "30") == "Accepted"
    assert store.get_int("HeartbeatInterval") == 30


def test_reboot_required():
    d = ConfigKeyDef("X", KeyType.INT, readonly=False, default="1", reboot_required=True)
    s = ConfigStore([d])
    assert s.set("X", "2") == "RebootRequired"
    assert s.get_int("X") == 2


def test_typed_getters(store):
    assert store.set("LocalPreAuthorize", "TRUE") == "Accepted"
    assert store.get_bool("LocalPreAuthorize") is True
    assert store.set("StopTxnSampledData", " Energy.Active.Import.Register , SoC ") == "Accepted"
    assert store.get_csl("StopTxnSampledData") == ["Energy.Active.Import.Register", "SoC"]
    assert store.get_csl("StopTxnAlignedData") == []
    with pytest.raises(TypeError):
        store.get_int("LocalPreAuthorize")


def test_listeners(store):
    seen = []
    store.add_listener(lambda k, v: seen.append((k, v)))
    store.set("HeartbeatInterval", "45")
    store.set("HeartbeatInterval", "45")  # unchanged: no callback
    store.set("NumberOfConnectors", "9")  # rejected: no callback
    assert seen == [("HeartbeatInterval", "45")]


def test_str_normalize_returns_stripped_value():
    from sme_ocpp_sim.protocol.base import ConfigKeyDef, KeyType
    from sme_ocpp_sim.protocol.v16.config_store import normalize
    free = ConfigKeyDef("X", KeyType.STR, False, "")
    assert normalize(free, "  abc ") == "abc"
    choice = ConfigKeyDef("Y", KeyType.STR, False, "a", allowed=frozenset({"a", "b"}))
    assert normalize(choice, " b ") == "b" and normalize(choice, "c") is None


@pytest.mark.parametrize("raw, want", [("10", "10"), (" 007 ", "7"), ("0", "0"), ("1_0", None), ("+5", None),
                                       ("-1", None), ("١٢", None), ("1.0", None), ("", None)])
def test_int_accepts_only_ascii_digits(raw, want):
    from sme_ocpp_sim.protocol.v16.config_store import normalize
    assert normalize(KEYS_BY_NAME["HeartbeatInterval"], raw) == want


def test_listener_exception_does_not_escape_set(caplog):
    from sme_ocpp_sim.protocol.base import ConfigStatus
    s = ConfigStore(KEYS)
    seen = []

    def bad(key, value):
        raise RuntimeError("boom")
    s.add_listener(bad)
    s.add_listener(lambda k, v: seen.append((k, v)))
    assert s.set("HeartbeatInterval", "42") is ConfigStatus.ACCEPTED
    assert s.value("HeartbeatInterval") == "42" and seen == [("HeartbeatInterval", "42")]
    assert "listener failed" in caplog.text


def test_keys_table_is_a_tuple_and_verified():
    assert isinstance(KEYS, tuple) and all(k.verified for k in KEYS)
