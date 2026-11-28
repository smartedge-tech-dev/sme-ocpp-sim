# Trace recording, analysis and replay

A **trace** is a file with every OCPP frame of a run, plus the events around them (connects, operator and
scenario actions, clock changes). Use it to keep the evidence of a test run, to compare runs, and to get a
summary report with `analyze`, and to check a CSMS with `replay`.

## Format `ocppsim-trace/1`

A trace is a JSON Lines file (one JSON object per line). Each line has a `type`:

| `type` | Content |
|---|---|
| `header` | Always the first line. `format: "ocppsim-trace"`, `version: 1`, `tool_version`, `created` (wall time), `time_scale`, `sim_start` (sim time when the recording started), `masked: true`, `part` and `chargers` (`id`, `ocpp_version`, `connectors`, `csms_url` without user and password) |
| `frame` | One OCPP-J frame with the fields of the frame log (`seq`, `ts`, `charger`, `direction`, `message_type`, `uid`, `action`, `raw`, `note`) plus `sim_t` (sim seconds since the recording started). `direction` is seen from the charger: `tx` = sent to the CSMS, `rx` = received |
| `event` | `ts`, `sim_t`, `charger` (`null` for engine-wide events), `event`, `detail`. Events: `connected`, `disconnected` (`detail.reason`), `connect_failed`, every operator / scenario action by its name (`plug`, `unplug`, `swipe`, `start`, `stop`, `fault`, `drop`, `send`, ...; `detail.since_seq` = the frame seq before the action), `clock` (time scale or pause changed) and `chargers` (a reload changed the charger list) |
| `footer` | The last line of a recording that was closed properly: `frames`, `events`, `dropped` (lines lost because the disk was too slow), `next` (the next part, see rotation) |

Secrets are masked exactly as in the frame log (for example a ChangeConfiguration `AuthorizationKey` value is
`********`). A trace never contains a password; [replay](#replay) takes the credentials as arguments.

A file without a header, such as a frame log export from `/api/frames.jsonl`, is read as **version 0**: every
line is a frame. `analyze` accepts both.

### Header charger entries and footers

Besides `id`, `ocpp_version`, `connectors` and `csms_url`, every `chargers` entry carries what a replay needs to
rebuild the charger: `vendor`, `model`, `serial`, `security_profile`, `subprotocol` (the WebSocket subprotocol),
`path` (the WebSocket path including the charger identity, e.g. `/ocpp/CP001`), `ev_model`, `seed`
(`traffic.seed`, which also seeds the vehicle streams) and `impairments` (the charger's configured impairments
at the start). None of them is a secret: `csms_url` has no user, password, query string or fragment (an
unparseable URL is written as `<invalid url>`), and the `connected` event's `detail.path` is the connection path
only. These fields are additive; the format version stays 1 and readers ignore fields they do not know.

**Footers are cumulative.** `frames`, `events` and `dropped` of a footer are the totals of the whole recording up
to that footer, not of its part; a reader takes the last footer (the sum over parts would count twice). A
footer may also carry `error` (the recording ended early, e.g. the next part could not be created; then `next` is
`null`). `analyze` reports `complete: false` when the last footer is missing, has an `error`, or its `next` part
file is missing.

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

A relative `trace.path` (and a relative `--trace FILE`) is resolved against the working directory of the
process, like the other file paths of the configuration (`ca_file`, `client_cert_file`), not against the config
file's directory. `--trace` does not change `trace.path`, so a config written back by the web UI keeps its own
value. The HTTP API (and the MCP `start_trace` tool) writes only into the trace directory and refuses a file name
that already exists with 409; the CLI `--trace FILE` takes any path and overwrites it.

If the next part cannot be opened during rotation, the recording ends with a footer with `next: null` and an
`error`. The writer flushes whenever its queue is empty and at least once a second, so a running recording can
be read.

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
  while an earlier CALL of the same side was still open (a CALL open for more than 60 s, or open when the
  charger disconnected, counts as unanswered instead). Frames and events are processed in time order; frame lines
  without `seq`, `ts` or `direction` are skipped and counted as bad lines.
- **Smart Charging**: the SetChargingProfile / ClearChargingProfile / UpdateDynamicSchedule calls of the CSMS
  with their answer status.

The analysis only reads frames and events, never the simulator state, so it works on a trace of a real charger
as well. There are no charts; build them from the JSON.

## Replay

`replay` plays **one charger** of a trace against a CSMS. It re-sends the CALLs that this charger sent
(`direction: tx`) on a new WebSocket connection, and compares the answers of the CSMS in this run with the
answers in the trace. Use it to check a CSMS against a recorded session, for example after a CSMS update, or to
reproduce a problem from a trace of a real charger.

```
python -m sme_ocpp_sim replay run1.jsonl --csms ws://csms.example:9000/ocpp \
    [--charger CP001] [--as CP900] [--timing original|scale:N|asap] [--max-gap SECONDS] [--session N] [--password PW] \
    [--out report.json] [--trace replay1.jsonl] [--json] [--fail-on-diff]
```

`--csms` is the CSMS URL without the charger id (the id is appended, as for a simulated charger). `--charger` is
needed when the trace has more than one charger. The OCPP version (and the WebSocket subprotocol) comes from the
trace header; for a v0 file without one, it is detected from the actions, or set with `--ocpp-version`. The detection is a guess (2.0.1 actions and payload shapes such as `transactionInfo`, `evse`, `eventType`); **pass `--ocpp-version` for a v0 trace**. The guess is listed in `notes`.

### What is replayed

Replay is **charger-side** and **frame-based**: the CSMS is the system under test, and the simulator plays the
recorded charger. The station state machine of the simulator is not used. What is sent is the recorded frames,
changed only where a second run needs it:

| Change | Rule |
|---|---|
| uniqueId | A new one for every CALL |
| Timestamps | Every string that is a full ISO 8601 date-time, at any depth of the payload (DataTransfer `data` included), is moved by one offset **per CALL, computed when the CALL is sent**: the earliest timestamp in that CALL becomes "now", and the spacing between timestamps inside the CALL is kept. So the timestamps do not drift into the future with `scale:N` or `asap`. Recorded answers to CSMS CALLs use the offset of the moment of answering |
| 1.6 `transactionId` | The id that the CSMS answers to StartTransaction now replaces the recorded id in the later frames (StopTransaction, MeterValues) |
| 2.0.1 / 2.1 `transactionId` | The charger creates the id, so the run suffix `-<replay id>` is added. An id that is too long for 36 characters keeps its head and the tail is cut. If two cut ids would become the same, a short hash of the full id is used instead; both cases are listed in `notes`. `--no-tx-suffix` keeps the recorded ids |
| `--as ID` | Connect with another charger identity (URL path and Basic Auth user). Payloads are not changed |

The CALLs are sent in order, one at a time: the next CALL is sent only after the answer or the call timeout
(`--call-timeout`, default 30 s), as OCPP-J requires. 2.1 SEND frames are sent again in order, without an answer.

The replay does not reproduce connection events: the whole replay uses one connection, and a disconnect in the
trace is not repeated. If the CSMS closes the connection, the replay stops, and the remaining CALLs are reported as
`not_sent`.

### Timing

| `--timing` | Gap between two CALLs |
|---|---|
| `original` (default) | The recorded gap, counted from the previous send. A slow answer moves the next CALL later |
| `scale:N` | The recorded gap divided by N (`scale:10` = 10 times faster) |
| `asap` | No gap: the next CALL is sent right after the answer |

`--max-gap SECONDS` (default: none, the recorded gaps are kept) caps each wait between two CALLs in `original` and `scale:N`, so that a trace with a long pause does not make the replay wait for hours. It is shown in the report next to `timing`.

### CALLs from the CSMS

The CSMS can send CALLs during a replay (for example TriggerMessage or GetConfiguration). They are answered with
the answer that the charger gave in the trace, matched by action and occurrence: the second GetConfiguration gets
the second recorded answer. After the last recorded answer, the last one is used again. A recorded CALLERROR is
answered with the same CALLERROR, and a CALL that was not answered in the trace is not answered. For an action that
the trace never answered, `--unknown-call` decides: `reject` (default) answers CALLERROR `NotImplemented`, and
`noreply` does not answer.

### Credentials

A trace is masked and has no password. Give the credentials as arguments: `--password` (Security Profile 1 / 2,
Basic Auth), `--ca-file` or `--insecure` for `wss://`, and `--client-cert` / `--client-key` (profile 3). The
report never contains the password.

### Sessions, stopping and limits

A trace can hold several connections (`connected` events) of the charger. By default all of them are replayed
merged on one connection, and a note says so; `--session N` (1-based) replays only the N-th one.

`stop` (or the end of the app) ends the wait for the answer of the CALL in flight: that CALL is reported as `closed`
and the remaining ones as `not_sent`. When the app shuts down, every running replay is stopped and its recording
gets its footer. The report keeps at most 1000 CSMS CALLs in `inbound`; `summary.inbound_overflow` counts the rest.

The HTTP API runs at most 4 replays at the same time (the next one gets 409), and refuses (409) an identity (`as_id`,
or the recorded charger id) that a charger of this simulator is using. `as_id` follows the rules of charger ids.
`csms_url` must be `ws://` or `wss://`. **The API connects to any host the caller names**: keep it bound to loopback
or behind authentication. `call_timeout` is at most 300 s over HTTP.

### Notes in the report

`notes` is mandatory reading when it is not empty. It lists: an incomplete trace (a part is missing) or lines the
recorder dropped (the replay may lack CALLs); a guessed OCPP version; more than one session of the charger; a
truncated or hash-resolved 2.0.1 `transactionId`; 1.6 StartTransaction without an answer in the trace, and
Stop / MeterValues of a transaction that started outside the trace; masked values (`********`) that are sent, or
answered, as they are (a secret the CSMS checks will not match); capped waits; CSMS CALLs beyond the list limit.
The `result` is not changed by notes.

### Report `ocppsim-replay/1`

`--out` writes the JSON report, `--json` prints it, and the default output is a text summary. `--trace` records
the replay into a new trace file. Its header entry has `replay_of` (source trace, charger, replay id).

| Field | Content |
|---|---|
| `id`, `charger`, `as`, `ocpp_version`, `csms_url`, `security_profile`, `timing` | What was replayed, and how |
| `result` | `match`, `diff` (at least one answer differs) or `error` (connection failed or lost, or the replay was stopped; `error` has the reason) |
| `summary` | `calls`, `sent`, `matched`, `differed`, `not_compared`, `timeouts`, `not_sent`, `inbound` (CSMS CALLs), `inbound_not_in_trace` |
| `calls[]` | For each CALL: `action`, `seq` in the trace, `recorded` and `replayed` outcome, `match` and `diff` |
| `inbound[]` | For each CSMS CALL: `action`, `occurrence`, `answer` (`result`, `error <code>`, `not_implemented`, `no_reply`), `in_trace`, `reused` |
| `notes` | What could not be rewritten, for example a 1.6 StartTransaction that got no `transactionId` now |

An outcome is `result` (with its status fields, such as `idTagInfo.status`), `error` (with the CALLERROR `code`),
`timeout`, `closed` or `not_sent`. A recorded outcome is `none` when the trace has no answer; such a CALL is not
compared. The status fields are the `status` keys and the keys that end in `Status`. Other fields, such as
`currentTime` or `interval`, change from run to run and are not compared.

A difference does not make the replay fail. Exit codes: 0 = done (also with differences), 1 = differences with
`--fail-on-diff`, 2 = error (the trace cannot be read, a bad option, the connection failed or was lost).

## HTTP API

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/trace` | Recording status (`recording`, `path`, `frames`, `events`, `dropped`) and the trace directory |
| POST | `/api/trace/start` | Body `{"file": "run1.jsonl"}` (optional; default `trace-<time>.jsonl`). 409 while a recording runs, or when the file already exists |
| POST | `/api/trace/stop` | Stop and write the footer. 409 when nothing is recording |
| GET | `/api/trace/files` | Trace files in the trace directory, newest first |
| GET | `/api/trace/files/{file}` | Download one trace file |
| GET | `/api/trace/files/{file}/analyze?charger=&format=json\|text` | Analysis of a trace file |
| GET | `/api/frames/analyze?<frame filters>&format=json\|text` | Analysis of the in-memory frame log (no connection events) |
| GET | `/api/frames.jsonl?format=v1` | Frame log export with a v1 header (default: plain entries, as before) |
| POST | `/api/replay` | Replay a trace file of the trace directory: body `{"file", "csms_url", "charger", "as_id", "timing", "max_gap", "session", "password", "ocpp_version", "unknown_call", "tx_suffix", "call_timeout", "verify_tls"}` (`file` and `csms_url` are required). It runs on its own connection, not on a simulated charger, and is recorded into `replay-<id>.jsonl` in the trace directory. Returns the `id` |
| GET | `/api/replay` | The replays of this app run |
| GET | `/api/replay/{id}` | State (`running` / `done`, `sent` / `total`) and, when done, the `report` |
| POST | `/api/replay/{id}/stop` | Stop after the CALL in flight |

MCP tools: `get_trace`, `start_trace`, `stop_trace`, `analyze_frames`, `start_replay`, `get_replay`.
