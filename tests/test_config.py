import pytest
from pydantic import ValidationError

from sme_ocpp_sim.config import ChargerConfig, SimConfig, parse_text

URL = "ws://localhost:8180/steve/websocket/CentralSystemService"


def _yaml(chargers: str, defaults: str = f'csms_defaults: {{ url: "{URL}" }}\n') -> SimConfig:
    return parse_text(defaults + "chargers:\n" + chargers)


def test_ids_stay_strings():
    cfg = _yaml('  - id: "0123"\n  - id: "1e5"\n')
    assert [c.id for c in cfg.chargers] == ["0123", "1e5"]


@pytest.mark.parametrize("raw", ["0123", "1", "1.5", "yes", "ON"])
def test_unquoted_non_string_id_rejected(raw):
    with pytest.raises(ValidationError, match="quoted string"):
        _yaml(f"  - id: {raw}\n")


def test_yaml_booleans_in_string_fields_not_misparsed():
    cfg = _yaml('  - id: "CP-1"\n    vendor: "ON"\n    model: "no"\n    id_tags: ["yes", "OFF"]\n')
    c = cfg.chargers[0]
    assert (c.vendor, c.model, c.id_tags) == ("ON", "no", ["yes", "OFF"])
    with pytest.raises(ValidationError, match="quoted string"):
        _yaml('  - id: "CP-1"\n    vendor: ON\n')
    with pytest.raises(ValidationError, match="quoted string"):
        _yaml('  - id: "CP-1"\n    id_tags: [yes]\n')


def test_config_key_values_coerced_to_wire_strings():
    cfg = _yaml('  - id: "CP-1"\n    config_keys: { HeartbeatInterval: 300, LocalPreAuthorize: yes,'
                ' MeterValuesSampledData: "SoC" }\n')
    assert cfg.chargers[0].config_keys == {"HeartbeatInterval": "300", "LocalPreAuthorize": "true",
                                           "MeterValuesSampledData": "SoC"}


@pytest.mark.parametrize("version,msg", [("2.1", "coming in v0.3"),
                                         ("1.5", "unknown OCPP version")])
def test_unavailable_version_rejected(version, msg):
    with pytest.raises(ValidationError, match=msg):
        ChargerConfig(id="CP-1", ocpp_version=version)
    with pytest.raises(ValidationError, match=msg):
        SimConfig.model_validate({"csms_defaults": {"url": URL, "ocpp_version": version}})


def test_password_not_in_repr():
    c = ChargerConfig(id="CP-1", password="s3cr3t-pw")
    assert "s3cr3t-pw" not in repr(c) and "s3cr3t-pw" not in str(c)
    assert c.password == "s3cr3t-pw"


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate charger id"):
        _yaml('  - id: "CP-1"\n  - id: "CP-1"\n')


def test_url_resolution_and_required():
    cfg = _yaml('  - id: "A"\n  - id: "B"\n    csms_url: "ws://other/ocpp"\n')
    assert cfg.resolve_csms(cfg.chargers[0]) == (URL, "1.6")
    assert cfg.resolve_csms(cfg.chargers[1]) == ("ws://other/ocpp", "1.6")
    with pytest.raises(ValidationError, match="no csms_url"):
        _yaml('  - id: "A"\n', defaults="")
    with pytest.raises(ValidationError, match="ws://"):
        ChargerConfig(id="A", csms_url="http://x")


def test_field_constraints():
    with pytest.raises(ValidationError):
        ChargerConfig(id="A", connectors=0)
    with pytest.raises(ValidationError):
        ChargerConfig(id="A", auth_mode="whatever")
    with pytest.raises(ValidationError):
        ChargerConfig(id="A", behaviors={})  # v0.2
    with pytest.raises(ValidationError, match="duplicate id_tags"):
        ChargerConfig(id="A", id_tags=["T1", "T1"])


def test_empty_config_and_defaults():
    cfg = parse_text("")
    assert cfg.chargers == [] and cfg.simulation.running and cfg.frame_log.size == 20000


@pytest.mark.parametrize("profile, url, password, ok", [
    (0, "ws://h/ocpp", None, True),
    (0, "ws://h/ocpp", "pw", False),
    (0, "wss://h/ocpp", None, False),
    (1, "ws://h/ocpp", "pw", True),
    (1, "ws://h/ocpp", None, False),
    (1, "wss://h/ocpp", "pw", False),
    (2, "wss://h/ocpp", "pw", True),
    (2, "ws://h/ocpp", "pw", False),
    (2, "wss://h/ocpp", None, False),
])
def test_security_profile_consistency(profile, url, password, ok):
    pw = f', password: "{password}"' if password else ""
    text = (f'csms_defaults: {{ url: "{url}", security_profile: {profile} }}\n'
            f'chargers:\n  - {{ id: "A"{pw} }}\n')
    if ok:
        assert parse_text(text).chargers[0].id == "A"
    else:
        with pytest.raises(ValidationError, match=f"security_profile {profile} needs"):
            parse_text(text)


def test_charger_id_colon_connectors_bound_and_float_boot_retry():
    from sme_ocpp_sim.config import MAX_CONNECTORS
    with pytest.raises(ValidationError, match="must not contain"):
        ChargerConfig(id="A:B")
    assert ChargerConfig(id="A", connectors=MAX_CONNECTORS).connectors == MAX_CONNECTORS
    with pytest.raises(ValidationError):
        ChargerConfig(id="A", connectors=MAX_CONNECTORS + 1)
    assert ChargerConfig(id="A", boot_retry_s=0.5).boot_retry_s == 0.5
    with pytest.raises(ValidationError):
        ChargerConfig(id="A", boot_retry_s=0)


def test_power_and_ev_fields():
    from sme_ocpp_sim.charging.ev import PRESETS
    from sme_ocpp_sim.config import parse_text

    c = ChargerConfig(id="A")
    assert (c.max_power_w, c.voltage_v, c.phases, c.line_to_line) == (6000.0, 200.0, 1, False)
    assert c.session.ev_model == "compact-40kwh" and (c.session.initial_soc, c.session.target_soc) == (30, 90)
    cfg = parse_text("""
csms_defaults: { url: "ws://x/ocpp" }
ev_models:
  - { id: "van", capacity_kwh: 75, max_ac_kw: 11, max_current_a: 16, phases: 3, curve: [[0, 1.0], [90, 0.5]] }
chargers:
  - { id: "A", phases: 3, voltage_v: 200, line_to_line: true, session: { ev_model: "van", target_soc: 80 } }
  - { id: "B" }
""")
    van = cfg.resolve_ev(cfg.chargers[0])
    assert (van.max_ac_w, van.phases, van.curve) == (11000.0, 3, ((0, 1.0), (90, 0.5)))
    assert cfg.resolve_ev(cfg.chargers[1]) is PRESETS["compact-40kwh"]
    with pytest.raises(ValidationError, match="unknown session.ev_model"):
        parse_text('csms_defaults: { url: "ws://x" }\nchargers: [{ id: "A", session: { ev_model: "nope" } }]')
    with pytest.raises(ValidationError, match="strictly increasing"):
        parse_text('ev_models: [{ id: "e", capacity_kwh: 1, max_ac_kw: 1, curve: [[50, 1], [10, 1]] }]')
    with pytest.raises(ValidationError):
        ChargerConfig(id="A", power_w=6000)  # M3 placeholder removed


def test_impairments_defaults_ranges_and_active():
    from sme_ocpp_sim.config import ImpairmentConfig
    imp = ImpairmentConfig()
    assert not imp.active and imp.direction == "both" and imp.down_s == 5 and imp.drop_mode == "abort"
    assert ImpairmentConfig(latency_ms=10).active and ImpairmentConfig(drop_every_s=30).active
    assert ImpairmentConfig(block={"Reset": "NoReply"}).active and not ImpairmentConfig(jitter_ms=0, seed=1).active
    assert ImpairmentConfig(latency_ms=10, jitter_ms=10).jitter_ms == 10
    for kw in ({"latency_ms": 10, "jitter_ms": 11}, {"drop_every_s": 0}, {"drop_every_s": 5, "drop_jitter_s": 5},
               {"down_s": -1}, {"direction": "up"}, {"drop_mode": "unplug"}, {"latency_ms": -1}):
        with pytest.raises(ValidationError):
            ImpairmentConfig(**kw)

def test_retry_limits_default_unlimited_and_non_negative():
    from sme_ocpp_sim.config import ReconnectConfig
    assert ReconnectConfig().max_retries is None and ChargerConfig(id="A").boot_max_retries is None
    assert ReconnectConfig(max_retries=0).max_retries == 0
    assert ChargerConfig(id="A", boot_max_retries=3).boot_max_retries == 3
    with pytest.raises(ValidationError):
        ReconnectConfig(max_retries=-1)
    with pytest.raises(ValidationError):
        ChargerConfig(id="A", boot_max_retries=-1)
