# Scenarios

A **scenario** is a YAML file that drives the simulated chargers through timed steps and checks how the CSMS
reacts: plug in, authorize, wait two hours, unplug, and assert that the CSMS accepted the transaction. It runs
headless for CI (verdict JSON, optional JUnit XML, exit code) or in the running app (HTTP API, MCP).

- A scenario is a separate file (`format: ocppsim-scenario/1`). It names chargers of the running config by id;
  it does not contain chargers or CSMS settings itself. One scenario runs at a time.
- Times follow the **simulation clock**: `after`, `wait` and `at` speed up with `time_scale` and stop while the
  simulation is paused. With `clock: {time_scale: 3600}` a two-hour session takes two wall seconds. The OCPP
  timestamps on the wire stay wall-clock time (the CSMS sees a shortened session, the energy follows sim time).
- Assertions wait in **wall** seconds (`within`, default 30 s): how fast a CSMS answers does not depend on the
  speed-up. `within_sim` waits in sim time instead.

Examples ship in the `scenarios/` folder of the repository; they run against `configs/mini-csms-demo.yaml`:

```bash
sme-ocpp-sim scenario run scenarios/plug-charge-unplug.yaml -c configs/mini-csms-demo.yaml --out verdict.json
```

## File format

```yaml
format: ocppsim-scenario/1
name: plug-charge-unplug
seed: 7                                   # recorded in the verdict
clock: {start_at: "2026-12-01T07:55:00+09:00", time_scale: 3600}   # both optional
params: {tag: "TAG001"}                   # ${tag} in any string below
tracks:
  - name: session
    charger: DEMO-16
    steps:
      - expect: {state: {online: true}}
        within: 30s
      - {at: "08:00", do: plug, connector: 1}
      - {after: 1m, do: swipe, connector: 1, id_tag: "${tag}"}
      - expect:
          frame: {dir: tx, action: StartTransaction}
          then: {message_type: CALLRESULT, payload: {idTagInfo.status: Accepted}}
        within: 10s
      - {do: wait, duration: 2h}
      - {do: unplug, connector: 1}
      - expect: {frame: {dir: tx, action: StopTransaction}}
until: "+4h"                              # optional hard end (sim time)
```

| Key | Meaning |
|---|---|
| `tracks` | Run in parallel; the steps of a track run in order on its `charger` |
| `for_each: {chargers: [A, B]}` | Instead of `charger`: one track per charger (`"*"` = every charger); `${charger}` / `${i}` are its id / 0-based index |
| `repeat: n` | Run the track's steps n times (`${round}` = 0-based round; step ids get `#1`, `#2` ...) |
| `clock.start_at` | Restart the sim clock at this date-time for the run (it anchors `at: "HH:MM"`) |
| `clock.time_scale` | Sim speed for this run (restored afterwards); `--time-scale` of the CLI overrides it |
| `until` | End of the run: a step still running then fails |

Every key is checked (a typo is an error with its position, e.g. `tracks.0.steps.2.do`); charger ids and
`${...}` names are checked against the running config before anything runs. A whole-string `"${n}"` keeps the
parameter's type (number, bool). Unquoted `08:00` is read as a time too.

### Timing

- `after: <duration>`: wait this long (sim) after the previous step, then run.
- `at: "HH:MM[:SS]"`: the next such sim clock time at or after the scenario start; `at: "+90m"`: sim time
  after the start; an ISO date-time is also accepted.
- A duration is a number of seconds or text: `500ms`, `30s`, `5m`, `2h`, `1h30m`, `1d`.

### Actions (`do:`)

| Action | Parameters | Effect (the same as the web UI button / HTTP route) |
|---|---|---|
| `plug` / `unplug` | `connector` (default 1) | EV cable in / out (unplug ends a transaction) |
| `swipe` | `connector`, `id_tag` | Present an id tag (start or stop) |
| `start` | `connector`, `id_tag` (default: the charger's first) | `plug` + `swipe` |
| `stop` | `connector` | Stop the transaction locally |
| `fault` / `clear_fault` | `connector`, `error_code` | Connector fault on / off |
| `connect` / `disconnect` | | Connection loop on / held off |
| `drop` | `mode` (`abort` / `close`), `reconnect_after_s` | Drop the connection now |
| `impair` | `impairments` (the whole block) | Replace the charger's [impairments](http-api.md#transaction-and-response-anomalies) live |
| `send` | `action` + `payload` or `raw`, `validate`, `timeout` | Inject a CALL / raw frame |
| `set_ev` | `connector`, `ev_model`, `initial_soc`, `target_soc` | The EV of the connector's next transaction (once) |
| `wait` | `duration` | Wait (sim time) |
| `log` | `message` | Write to the event log |

A rejected action (unknown connector, no transaction to stop ...) is a step **error**.

### Assertions

```yaml
- expect: {frame: {dir: tx, action: MeterValues, payload: {meterValue.0.sampledValue.0.value: {gt: 0}}}}
- expect: {state: {connector: 1, status: Charging, power_w: {ge: 3000}, has_transaction: true}}
  within_sim: 10m
- expect_not: {dir: tx, action: StartTransaction}    # passes when no such frame shows up in the window
  within: 20s
- await_csms: {action: RemoteStartTransaction, prompt: "Start charging on CP001 from the CSMS"}
  within: 5m
```

- `expect.frame`: `dir` (`tx` = sent by the charger, `rx` = received), `action`, `message_type` (`CALL` /
  `CALLRESULT` / `CALLERROR`), `payload` matchers. `then:` checks the answer to the matched CALL (same message
  id): `message_type` and `payload` (a CALLERROR's payload is `errorCode` / `errorDescription` /
  `errorDetails`).
- `expect.state`: `connector`, `status`, `has_transaction`, `power_w`, `soc_pct`, `meter_wh`, `online`,
  `connected`.
- `expect_not`: a frame filter; the step fails as soon as a match appears in the window.
- `await_csms`: waits for a CALL from the CSMS (an operator action on the CSMS side); `prompt` is shown in the
  status and the event log while it waits.
- Frames are searched from the start of the track's **previous action step**, so an answer that came before the
  assertion started is found. Two assertions in a row look at the same frames.
- `on_fail: stop` (default) ends the whole scenario at a failed or rejected step; `continue` goes on.

**Matchers** (Q6: built in, no JSONPath, no code): a path is dotted, integer parts index lists
(`meterValue.0.sampledValue.0.value`). The value is a literal (equality) or operators that must all hold:
`in: [...]`, `gt` / `ge` / `lt` / `le: n` (numeric strings compare as numbers), `re: "regex"` (search),
`exists: true|false`.

## Verdict

```json
{"format": "ocppsim-verdict/1", "scenario": "plug-charge-unplug", "result": "pass", "error": null, "seed": 7,
 "started": "2026-11-28T17:12:16+09:00", "wall_s": 2.1, "sim_s": 7423.0, "tool_version": "0.3.0",
 "trace": null, "steps": [{"id": "4", "track": "session", "kind": "expect", "status": "pass",
 "t_wall": 0.02, "t_sim": 60.0, "message": "StartTransaction: seq 7, answer seq 8", "evidence": [7, 8]}]}
```

`result`: `pass`, `fail` (an assertion failed), `error` (an action was rejected, or the scenario could not
start), `aborted` (stopped from outside). Step `status`: `pass` / `fail` / `error` / `skipped` / `aborted`.
`evidence` are frame sequence numbers (the frame log / `--trace` file).

## Command line

```bash
sme-ocpp-sim scenario run FILE [-c CONFIG] [--out verdict.json] [--junit junit.xml] [--trace frames.jsonl]
                              [--time-scale N] [-v] [--log-format json]
sme-ocpp-sim scenario validate FILE [-c CONFIG]
```

`run` starts the engine (and the Mini CSMS if the config enables it) without the web UI, runs the scenario,
writes the verdict (stdout without `--out`) and exits **0** pass, **1** fail, **2** error / aborted / invalid
scenario or config. `--junit` writes one test case per assertion (and per failed action); `--trace` writes the
frames of the scenario's chargers as JSON lines (the frame log export format).

## HTTP API and MCP

| Method | Path | MCP tool | Meaning |
|---|---|---|---|
| POST | `/api/scenarios/run` | `run_scenario` | Body: the scenario YAML (or JSON) text; starts it (409 while one runs) |
| GET | `/api/scenarios/current` | `get_scenario_status` | `state` (`idle` / `running` / `done`), track progress, step counts, prompts |
| POST | `/api/scenarios/abort` | `abort_scenario` | Stop it (result `aborted`) |
| GET | `/api/scenarios/result?format=json\|junit` | `get_scenario_result` | Verdict of the finished scenario |
| POST | `/api/frames/expect` | `expect_frame` | One-shot: wait up to `within` s for a matching frame after `since` |

The web UI has no scenario panel yet; it shows the scenario's `log` lines and its start / result in the event log.

## Limits

- Generator tracks (`generate:`, the traffic generator) come with v0.3 M4; `seed` is only recorded until then.
- `send` injects charger → CSMS CALLs; CSMS-side steps are `await_csms` (or the Mini CSMS API).
- The trace file is the current frame log format; the versioned trace format comes later.
