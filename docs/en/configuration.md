# Configuration

The simulator reads one YAML file (`-c FILE`). Every block is optional; unknown fields are rejected. A complete
example is [`configs/demo-site.yaml`](https://github.com/smartedge-tech-dev/sme-ocpp-sim/blob/main/configs/demo-site.yaml).

```yaml
simulation:
  tick_seconds: 1
  time_scale: 1
  running: true

frame_log:
  size: 20000

csms_defaults:
  url: "ws://localhost:8180/steve/websocket/CentralSystemService"
  ocpp_version: "1.6"
  security_profile: 0
  reconnect: { min_s: 1, max_s: 60, jitter: 0.2 }

ev_models:
  - id: "demo-60kwh"
    capacity_kwh: 60
    max_ac_kw: 11
    max_current_a: 16
    phases: 3

chargers:
  - id: "CP-0001"
    max_power_w: 6000
    id_tags: ["04A1B2C3", "TAG-0002"]
    config_keys:
      HeartbeatInterval: 300
```

!!! warning "Quote ids, tags and passwords"
    In YAML 1.1 unquoted `0123`, `1e5`, `ON`, `OFF`, `yes` and `no` are not strings. Identity and text fields
    (`id`, `id_tags`, `password`, URLs, `vendor`, ...) only accept quoted strings and ask you to add quotes,
    instead of silently turning `0123` into `83`.

## Saving from the web UI { #saving-from-the-web-ui }

Changes made in the UI apply immediately (only the chargers that changed reconnect) and can be saved back to
the YAML file. Saving replaces every top-level block that changed (`simulation`, `frame_log`, `csms_defaults`,
`ev_models`, `chargers`) as a whole:

- Comments **outside** a changed block are kept, as are the file's line endings and a UTF-8 BOM.
- Comments **inside** a changed block are lost. The whole `chargers:` block is rewritten when any charger
  changes, so **don't put comments inside individual charger entries** — put them above the `chargers:` key.
- Values equal to their default are not written out.
- If a block cannot be spliced (unusual layout), the whole document is re-dumped and all comments are lost; a
  warning is logged.

Passwords are never returned by the HTTP API: they appear as `********`. Sending a configuration back with the
mask keeps the stored password.

## simulation

| Field | Default | Meaning |
|---|---|---|
| `tick_seconds` | `1.0` | Step of the power model in seconds (> 0) |
| `time_scale` | `1.0` | Speeds up the power model (ramp, energy register, SoC) only. Charging schedules and OCPP timing always use the wall clock |
| `running` | `true` | `false` freezes the power model; the OCPP sessions keep running |

## frame_log

| Field | Default | Meaning |
|---|---|---|
| `size` | `20000` | OCPP frames kept in memory for all chargers together (≥ 100) |

## csms_defaults

| Field | Default | Meaning |
|---|---|---|
| `url` | none | CSMS WebSocket URL (`ws://` or `wss://`); the charger id is appended as the last path segment. Required unless every charger has `csms_url` |
| `ocpp_version` | `"1.6"` | Only `"1.6"` in v0.1; `"2.0.1"` / `"2.1"` are rejected as coming in a later version |
| `security_profile` | `0` | `0`, `1` or `2`; every charger is checked against it, see [Security profiles](security-profiles.md) |
| `reconnect.min_s` | `1.0` | First reconnect delay in seconds |
| `reconnect.max_s` | `60.0` | Maximum reconnect delay (≥ `min_s`); the delay grows exponentially |
| `reconnect.jitter` | `0.2` | Random spread of the delay (0–1) |

## ev_models

Custom EV models, referenced by `session.ev_model`. An entry overrides a built-in preset with the same id.
Built-in presets: `kei-20kwh` (20 kWh, 3 kW, 15 A, 1 phase), `compact-40kwh` (40 kWh, 6 kW, 30 A, 1 phase),
`midsize-60kwh` (60 kWh, 11 kW, 16 A, 3 phases).

| Field | Default | Meaning |
|---|---|---|
| `id` | required | Unique id (quoted string) |
| `capacity_kwh` | required | Battery capacity |
| `max_ac_kw` | required | Maximum AC charging power |
| `max_current_a` | `32.0` | Maximum current per phase |
| `max_dc_kw` | none | Accepted for forward compatibility; DC charging is not modelled in v0.1 |
| `phases` | `1` | Phases the on-board charger uses (1, 2 or 3) |
| `min_current_a` | `6.0` | Below this current per phase the EV cannot charge (≤ `max_current_a`) |
| `efficiency` | `0.9` | Fraction of the energy that reaches the battery (0–1] |
| `curve` | built-in | List of `[SoC %, fraction of maximum]` pairs: the charging taper |

## chargers

| Field | Default | Meaning |
|---|---|---|
| `id` | required | Charge point id (quoted string). Used as the last URL path segment and as the Basic auth user-id, so it must not contain `:`; `bulk` is reserved |
| `csms_url` | `csms_defaults.url` | Per-charger CSMS URL |
| `ocpp_version` | `csms_defaults.ocpp_version` | Per-charger OCPP version |
| `password` | none | Basic auth password (security profile 1 / 2) |
| `ca_file` | system trust store | Extra CA bundle (PEM) to verify the CSMS certificate on `wss://` |
| `verify_tls` | `true` | `false` skips certificate verification (testing only; a warning is logged) |
| `require_subprotocol` | `true` | `false` keeps the connection when the CSMS does not echo the `ocpp1.6` subprotocol |
| `vendor` | `"SmartEdge"` | BootNotification `chargePointVendor` (max. 20 characters) |
| `model` | `"SIM-AC6"` | `chargePointModel` (max. 20) |
| `serial` | none | `chargePointSerialNumber` (max. 25) |
| `firmware` | `"0.1.0"` | `firmwareVersion` (max. 50) |
| `connectors` | `1` | Number of connectors (1–32); all share the supply settings and `max_power_w` below |
| `config_keys` | `{}` | Initial OCPP configuration keys, e.g. `HeartbeatInterval: 300`; numbers and booleans may be unquoted |
| `auth_mode` | `local_then_csms` | `local_only` (only `id_tags`), `csms_only` (always Authorize), `local_then_csms` (`id_tags` first, then Authorize), `accept_all` |
| `id_tags` | `[]` | Locally accepted id tags (max. 20 characters each, no duplicates) |
| `boot_retry_s` | `60.0` | Boot retry delay when the CSMS answers Pending / Rejected with interval 0 |
| `max_power_w` | `6000.0` | Hardware maximum power of each connector |
| `voltage_v` | `200.0` | Nominal supply voltage |
| `phases` | `1` | Supply phases (1, 2 or 3) |
| `line_to_line` | `false` | `true`: `voltage_v` is line-to-line on a 3-phase supply (e.g. Japanese 3-phase 200 V delta); per-phase voltage = `voltage_v / √3` |
| `ramp_up_w_per_s` | none | Power ramp-up in W per simulated second (none = step change) |
| `ramp_down_w_per_s` | none | Power ramp-down (none = step change, so a lowered limit applies within one tick) |
| `session.ev_model` | `compact-40kwh` | The EV plugged into the connectors (preset or `ev_models` id) |
| `session.initial_soc` | `30.0` | SoC (%) at the start of every transaction |
| `session.target_soc` | `90.0` | When reached, the EV stops accepting power (SuspendedEV) |
| `meter_sample_interval_s` | key default | Default `MeterValueSampleInterval` when `config_keys` does not set it |

Not in v0.1: charger templates, the 2.0.1 `evses` tree and `behaviors` (planned for v0.2).
