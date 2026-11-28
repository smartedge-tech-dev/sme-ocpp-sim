# Trace recording and analysis

A **trace** is a file with every OCPP frame of a run, plus the events around them (connects, operator and
scenario actions, clock changes). Use it to keep the evidence of a test run, to compare runs, and to get a
summary report with `analyze`. Replay of a trace against a CSMS is planned for a later release; the format
already carries what replay needs.

## Format `ocppsim-trace/1`

A trace is a JSON Lines file (one JSON object per line). Each line has a `type`:

| `type` | Content |
|---|---|
| `header` | Always the first line. `format: "ocppsim-trace"`, `version: 1`, `tool_version`, `created` (wall time), `time_scale`, `sim_start` (sim time when the recording started), `masked: true`, `part` and `chargers` (`id`, `ocpp_version`, `connectors`, `csms_url` without user and password) |
| `frame` | One OCPP-J frame with the fields of the frame log (`seq`, `ts`, `charger`, `direction`, `message_type`, `uid`, `action`, `raw`, `note`) plus `sim_t` (sim seconds since the recording started). `direction` is seen from the charger: `tx` = sent to the CSMS, `rx` = received |
| `event` | `ts`, `sim_t`, `charger` (`null` for engine-wide events), `event`, `detail`. Events: `connected`, `disconnected` (`detail.reason`), `connect_failed`, every operator / scenario action by its name (`plug`, `unplug`, `swipe`, `start`, `stop`, `fault`, `drop`, `send`, ...; `detail.since_seq` = the frame seq before the action), `clock` (time scale or pause changed) and `chargers` (a reload changed the charger list) |
| `footer` | The last line of a recording that was closed properly: `frames`, `events`, `dropped` (lines lost because the disk was too slow), `next` (the next part, see rotation) |

Secrets are masked exactly as in the frame log (for example a ChangeConfiguration `AuthorizationKey` value is
`********`). A trace never contains a password; a later replay takes the credentials as arguments.

A file without a header, such as a frame log export from `/api/frames.jsonl`, is read as **version 0**: every
line is a frame. `analyze` accepts both.

## Recording

| Way | What is recorded | File |
|---|---|---|
| Config `trace.path` | Every charger, from the start of the simulator to its stop | `trace.path` (overwritten) |
| `sme-ocpp-sim --trace FILE` | The same (overrides `trace.path`) | `FILE` |
| Web UI **Record trace** button (frame log toolbar), `POST /api/trace/start` | Every charger, until **Stop recording** / `POST /api/trace/stop` | The trace directory |
| Scenario started from the web UI / API | The scenario's chargers | The trace directory, `scenario-<name>-<time>.jsonl`; the verdict's `trace` field names it |
| `sme-ocpp-sim scenario run ... --trace FILE` | The scenario's chargers | `FILE` |

```yaml
trace:
  path: traces/run.jsonl   # null (default) = no recording from the start
  rotate_mb: 100           # start a new part at this size; null = never
```

The **trace directory** is the directory of `trace.path` when it is set, otherwise `traces/` beside the config
file (Windows exe: `~/.sme-ocpp-sim/traces`). The HTTP API only writes and serves files in that directory.

**Rotation**: when a part reaches `rotate_mb`, it is closed with a footer whose `next` names the next part
(`run.jsonl` → `run.1.jsonl` → `run.2.jsonl` ...). Every part starts with its own header. `analyze` given the
first part reads the whole chain.

The recorder never slows the simulator down: lines go through a bounded queue to a background writer. If the
disk cannot keep up, lines are dropped and counted in the footer's `dropped`.

## analyze

```
sme-ocpp-sim analyze traces/run.jsonl                 # text summary
sme-ocpp-sim analyze traces/run.jsonl --json          # ocppsim-analysis/1 JSON
sme-ocpp-sim analyze frames.jsonl --charger CP001 --ocpp-version 2.0.1
```

`--ocpp-version` sets the version of chargers a v0 file does not name (default: detected from the actions).
The report, per charger:

- **Actions** per direction: CALL count, CALLRESULT, CALLERROR by error code, unanswered CALLs and the
  response latency p50 / p95 / max (ms; CALL and response paired by message id).
- **Connection**: connect / disconnect counts and connected durations (v1 traces), the BootNotification
  result sequence, the Heartbeat interval distribution.
- **Transactions**: start, end, duration, Δ meter (Wh), stop reason. OCPP 2.0.1 / 2.1: `seqNo` gaps and
  duplicates per transaction.
- **Protocol anomalies**: unparseable frames, responses without a CALL, message ids used twice, a CALL sent
  while an earlier CALL of the same side was still open.
- **Smart Charging**: the SetChargingProfile / ClearChargingProfile / UpdateDynamicSchedule calls of the CSMS
  with their answer status.

The analysis only reads frames and events, never the simulator state, so it works on a trace of a real charger
as well. There are no charts; build them from the JSON.

## HTTP API

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/trace` | Recording status (`recording`, `path`, `frames`, `events`, `dropped`) and the trace directory |
| POST | `/api/trace/start` | Body `{"file": "run1.jsonl"}` (optional; default `trace-<time>.jsonl`). 409 while a recording runs |
| POST | `/api/trace/stop` | Stop and write the footer. 409 when nothing is recording |
| GET | `/api/trace/files` | Trace files in the trace directory, newest first |
| GET | `/api/trace/files/{file}` | Download one trace file |
| GET | `/api/trace/files/{file}/analyze?charger=&format=json\|text` | Analysis of a trace file |
| GET | `/api/frames/analyze?<frame filters>&format=json\|text` | Analysis of the in-memory frame log (no connection events) |
| GET | `/api/frames.jsonl?format=v1` | Frame log export with a v1 header (default: plain entries, as before) |

MCP tools: `get_trace`, `start_trace`, `stop_trace`, `analyze_frames`.
