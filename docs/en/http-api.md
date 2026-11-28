# HTTP API

The web UI uses the same HTTP API that your test scripts can call. Base URL: `http://127.0.0.1:8082`.
Errors: an unknown charger is `404`; invalid input, or an operation the current state does not allow, is `400`
(request validation errors are `400` as well). A DataTransfer the CSMS does not answer in time is `504`, one it
answers with CALLERROR is `502`.

Successful requests answer `{"ok": true, ...}` (GET routes return the data itself).

## Common routes

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/version` | Version |
| GET | `/api/state` | Full state snapshot: chargers, connectors, values |
| GET / PUT | `/api/config` | Current configuration (PUT applies a new one; only changed chargers reconnect) |
| POST | `/api/config/save` | Write the configuration back to the YAML file (see [Configuration](configuration.md#saving-from-the-web-ui)); `500` when the file cannot be written |
| POST | `/api/simulation` | Run / pause and time scale: `{"running": true, "time_scale": 10}` (both optional; other fields are rejected) |
| GET / PUT | `/api/csms-defaults` | The `csms_defaults` block (PUT replaces it and reloads) |
| GET / PATCH | `/api/chargers/{id}/settings` | Connection / authorization settings of one charger (see below) |
| GET | `/api/chargers/{id}/configuration` | Run-time configuration keys as GetConfiguration sees them; OCPP 2.0.1: the Device Model variables (see below) |
| POST | `/api/devices` | Add a charger |
| GET / PUT | `/api/devices/{id}/config` | One charger's configuration (PUT replaces the whole item) |
| POST | `/api/devices/{id}/duplicate` | Copy a charger (the id is incremented) |
| DELETE | `/api/devices/{id}` | Remove a charger |

Passwords are always returned as `********`; sending the mask back keeps the stored password. Since a PUT
replaces the whole item, an omitted `password` means no password.

`/api/chargers/{id}/settings` covers `csms_url`, `ocpp_version`, `password`, `ca_file`, `client_cert_file`, `client_key_file`,
`verify_tls`,
`id_tags` and `config_keys`, plus the `effective` CSMS URL and OCPP version. PATCH changes only the fields
it contains; `null` unsets a field (`id_tags` / `config_keys` become empty; `verify_tls` must be true or
false). The password mask round-trips as above. Changes are applied with a configuration reload and are
written to the file only by `POST /api/config/save`.

For an OCPP 2.0.1 charger `/api/chargers/{id}/configuration` returns `{"keys": [], "ocpp_version": "2.0.1",
"variables": [...]}`: one entry per Device Model variable with `component`, `component_instance`, `evse_id`,
`connector_id`, `variable`, `variable_instance`, `data_type`, `mutability`, `unit`, `min_limit`, `max_limit`,
`values_list`, `reboot_required` and `attributes` (the `Actual` / `Target` / `MinSet` / `MaxSet` values it has;
`null` for a WriteOnly variable), incl. the values the CSMS changed with SetVariables. There is no route to change
a variable: the CSMS uses SetVariables, and the YAML file sets the start values (`variables:`).

## Excel and templates { #excel-and-templates }

See [Excel import / export and charger templates](excel-templates.md) for the workbook layout and the template rules.

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/config/xlsx` | The chargers, config keys, variables and EV models as an Excel workbook (passwords as `********`) |
| GET | `/api/config/xlsx-template` | A blank workbook with the column headers |
| POST | `/api/config/import` | Multipart field `file`: apply a workbook (all or nothing). `{"ok": true, "warnings": [...]}`, or `400` with `errors`: one message per problem, naming sheet, row and column |
| GET | `/api/templates` | Charger templates: `name`, `title`, `description`, `params`, `source` (`builtin` / `user`) |
| POST | `/api/templates/reload` | Read the user template directory again: `{"dir", "loaded", "errors", "templates"}` |
| POST | `/api/templates/from-device/{id}` | `{"name", "title"?, "description"?, "overwrite"?}`: save a charger's template fields as a user template (`409` when it exists and `overwrite` is not set) |
| POST | `/api/chargers/bulk/create` | `{"id": "CP-0001", "count": 10, "template"?, "params"?, "password"?, "fields"?}`: add `count` chargers in one reload; the ID and a password ending in digits count up, `fields` are further charger fields for each (`csms_url`, `ocpp_version`, `id_tags` …). `409` when an ID exists already (nothing is added) |

## Charger operations

| Method | Path | Body |
|---|---|---|
| GET | `/api/chargers/{id}` | — the charger's snapshot plus `fault_codes`, the fault codes of its OCPP version |
| POST | `/api/chargers/{id}/connect` | — (starts the connection loop) |
| POST | `/api/chargers/{id}/disconnect` | — (stays disconnected until `connect`; transactions survive like a network loss) |
| POST | `/api/chargers/{id}/plug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/unplug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/swipe` | `{"connector": 1, "id_tag": "04A1B2C3"}` — `id_tag` is required |
| POST | `/api/chargers/{id}/start` | `{"connector": 1, "id_tag": "04A1B2C3"}` — plug in and swipe; `id_tag` defaults to the first configured tag (`400` when there is none); `400` when the connector already has a transaction or is faulted |
| POST | `/api/chargers/{id}/stop` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/fault` | `{"connector": 1, "error_code": "OtherError"}` — one of the charger's `fault_codes` (1.6: `ChargePointErrorCode`; 2.0.1: the fault values of `ReasonEnumType`), `400` otherwise; ends a running transaction (1.6: reason `Other`; 2.0.1: `stoppedReason` = the code) and reports `Faulted` |
| POST | `/api/chargers/{id}/clear-fault` | `{"connector": 1}` — back to Available / Preparing |
| POST | `/api/chargers/{id}/datatransfer` | `{"vendor_id": "...", "message_id": "...", "data": "..."}` (charger → CSMS) |
| GET / PUT | `/api/chargers/{id}/impairments` | `{"block": {"RemoteStartTransaction": "NotSupported"}, "latency_ms": 200, ...}` — command blocking, latency, periodic drops, see [below](#command-blocking-and-frame-injection) |
| POST | `/api/chargers/{id}/drop` | `{"mode"?: "abort" \| "close", "reconnect_after_s"?}` — drop the connection now, see [below](#network-impairments) |
| POST | `/api/chargers/{id}/send` | `{"action": "Heartbeat", "payload": {}, "validate": true}` or `{"raw": "[2,\"id-1\",\"Heartbeat\",{}]"}` — frame injection, see [below](#command-blocking-and-frame-injection) |
| POST | `/api/chargers/bulk/connect` | `{"ids": ["CP-0001", "CP-0002"]}`, `["CP-0001"]` or `{"all": true}` |
| POST | `/api/chargers/bulk/disconnect` | same as above |

Bulk operations are not atomic: chargers are handled one after the other, and an error stops the request with
the earlier ones already changed.

## Command blocking and frame injection { #command-blocking-and-frame-injection }

**Command blocking** (`impairments.block` of a charger, see [Configuration](configuration.md#chargers)):
`GET /api/chargers/{id}/impairments` returns the block, `PUT` replaces it as a whole (`400` for an action the
charger's OCPP version does not receive; `GET /api/chargers/{id}` lists those it does as `csms_actions`). The
change is applied to the running charger at once, without a reconnect, and is written to the file by
`POST /api/config/save`. A blocked CSMS → charger CALL is answered with CALLERROR `NotImplemented` or
`NotSupported` (the same wire names in OCPP-J 1.6 and 2.0.1), or, with `NoReply`, not at all, so the CSMS runs
into its own timeout. The frame log marks the received CALL with `blocked: <code>` / `blocked: no reply`, and the
charger snapshot has `impaired: true` (the UI shows `!` on the badge). In the UI: **Impairments** in the
charger detail panel.

### Transaction and response anomalies

Also in the `impairments` block and applied live (v0.3): `remote_start: accept_no_tx` (the remote start is
`Accepted`, but no transaction follows and the connector state does not change), `meter` (`drop_ratio`,
`reorder_window`, `end_after_stop`, `voltage_noise_v`) and `response_delay_ms` (action → ms the answer is held),
see [Configuration](configuration.md#chargers). What the CSMS sees:

- `accept_no_tx`: an accepted RemoteStartTransaction / RequestStartTransaction and then nothing — no
  StartTransaction / TransactionEvent `Started`, no StatusNotification.
- `drop_ratio`: missing periodic MeterValues (1.6) / `Updated` events (2.0.1); in 2.0.1 the `seqNo` of the
  transaction has gaps.
- `reorder_window`: periodic meter messages arrive in bursts in a shuffled order; timestamps (and 2.0.1 `seqNo`)
  are not monotonic.
- `end_after_stop`: a meter message of the transaction after its StopTransaction / Ended event.
- `voltage_noise_v`: Voltage values that jitter around the nominal supply voltage.
- `response_delay_ms`: the answer to that action arrives late (or after the CSMS's timeout); other traffic of
  the charger, including heartbeats, is not delayed.

The random draws (drops, order, noise) use `seed` (default: a CRC32 of the charger id), one stream per connector,
so a run is reproducible. The streams go on across live edits of the block (only a changed `seed` restarts them)
and start over when the charger restarts. Messages held by `reorder_window` are sent as soon as the window is set
to 0 / 1 and whenever the transaction ends; a held 2.0.1 event gets its `offline` flag when it is queued (the
connection at that moment) and keeps its original timestamp and `seqNo`. A charger restart drops held messages.
With `end_after_stop` and without `meter.begin_end_messages`, OCPP 1.6 sends one extra `Sample.Periodic`
MeterValues, read at the stop, after the StopTransaction (the late end reading).

### Network impairments

The same `impairments` block also holds the latency (`latency_ms`, `jitter_ms`, `direction`, `seed`) and the
periodic drops (`drop_every_s`, `drop_jitter_s`, `drop_mode`, `down_s`), see
[Configuration](configuration.md#chargers). `PUT` takes the whole block (fields left out get their defaults;
`400` for out-of-range values, e.g. `jitter_ms` > `latency_ms`) and applies it to the open connection without a
reconnect: the next frame already gets the new delay.

`POST /api/chargers/{id}/drop` drops the connection now and returns `{"ok": true, "mode", "reconnect_after_s"}`.
`mode` defaults to `drop_mode`, `reconnect_after_s` (0 – 86400) to `down_s`; `409` while the charger is not
connected. The charger reconnects after that pause instead of its back-off. A simulated drop is **not** a
failed connection: it never counts against `reconnect.max_retries` / `boot_max_retries`, even before an accepted
boot. Transactions and queued messages are kept. The snapshot shows `last_error.kind` `simulated_drop` until
the next accepted boot and counts the drops in `drops`; `impaired` is `true` while any impairment is on. In the
UI: **Impairments** in the charger detail panel (also **Drop now**).

**Frame injection** (`POST /api/chargers/{id}/send`) is an expert entry for testing how a CSMS handles messages
the simulator would not send on its own:

- `{"action", "payload", "validate": true, "timeout"}` sends one CALL and returns `{"ok": true, "result": {...}}`,
  or `{"ok": false, "error": {"code", "description", "details"}}` for a CALLERROR; `504` on timeout (`timeout`
  in seconds, default the call timeout). With `validate: true` the action must be a charger → CSMS action of the
  charger's version and the payload must match its request schema (`400` otherwise); an answer that does not
  match the response schema adds a `warning`. `validate: false` sends any action and any JSON payload.
- `{"raw": "<text>"}` writes the text to the WebSocket verbatim and waits for nothing; an answer to it shows up
  in the frame log as an unsolicited response.
- The station state never changes: an injected StartTransaction creates no transaction, an injected
  StatusNotification changes no connector. Injected frames are marked `injected` / `injected (raw)` in the
  frame log. `409` while the charger is not connected.
- One CALL is outstanding per connection (OCPP-J), so the charger's own CALLs (Heartbeat, MeterValues, ...)
  queue behind an injected CALL until it is answered or times out; `timeout` is therefore at most 60 s.
- `raw` and the serialized `payload` are limited to 1,000,000 characters (`400` / `413`). A raw frame whose
  uniqueId is that of a pending charger CALL is refused with `409` (its answer would resolve that call).

## Smart Charging

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/chargers/{id}/profiles?connector=` | Installed charging profiles |
| GET | `/api/chargers/{id}/composite?connector=&duration=&unit=` | The GetCompositeSchedule answer computed locally (`connector` 0 = whole charge point; `evse` is accepted as an alias) |
| POST | `/api/chargers/{id}/external-limit` | Body `{"connector": 0, "limit_w": 7000}` (`limit_w` `null` = remove). An external (energy management) limit: OCPP 2.0.1 installs a ChargingStationExternalConstraints profile and sends NotifyChargingLimit / ClearedChargingLimit; OCPP 1.6 answers 400 (not supported) |

For an OCPP 2.0.1 charger `connector` is the EVSE id (0 = the whole station).

## Frame log

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/frames?since=&until=&charger=&action=&dir=&limit=` | OCPP frames with `since < seq <= until`, filtered by charger, action and direction |
| GET | `/api/frames.jsonl?...` | The same as a streamed JSON Lines download (without `limit`: every matching frame in the buffer) |

`/api/frames` returns `{"frames": [...], "last_seq": n, "truncated": bool}`: at most the newest `limit`
matching frames (default 500, maximum 5000), oldest first. `truncated: true` means older matching frames were
left out; `last_seq` is the newest sequence number of the whole log (filters ignored).

The frame log contains OCPP frames only, never HTTP headers or passwords.

## Scenarios { #scenarios }

| Method | Path | Meaning |
|---|---|---|
| POST | `/api/scenarios/run` | Start a scenario; body: its YAML (or JSON) text. 400 invalid / unknown charger, 409 one is running |
| GET | `/api/scenarios/current` | Status of the current / last scenario (`{"state": "idle"}` when none) |
| POST | `/api/scenarios/abort` | Stop the running scenario (409 when none runs) |
| GET | `/api/scenarios/result?format=json\|junit` | Verdict of the finished scenario (404 none, 409 still running) |
| POST | `/api/frames/expect` | Wait up to `within` s (max 300) for a frame after `since` matching `charger` / `dir` / `action` / `message_type` / `payload` matchers; `{"matched": bool, "frame": ...}` |

See [Scenarios](scenarios.md) for the file format and the verdict.

## Traffic generator { #traffic }

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/traffic` | State, `traffic:` settings, counters, active sessions, total power, recent events |
| PUT | `/api/traffic` | Replace the `traffic:` block (reload; 400 invalid) |
| POST | `/api/traffic/start` | Start the standing traffic now, not saved (400 no tag pool / chargers) |
| POST | `/api/traffic/stop` | Stop it; its sessions are stopped and unplugged (409 not running) |
| GET | `/api/traffic/plan?hours=&charger=&limit=&start=` | Preview the planned arrivals (deterministic for a seed and start) |

See [Traffic generator](traffic.md).

## Mini CSMS { #mini-csms }

Routes of the built-in [Mini CSMS](mini-csms.md). Station ids are the ids the stations connected with.

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/csms` | Status (`enabled`, `running`, `error`, `url`, `versions`, ...), the stations (`chargers`) and refused connections (`rejections`) |
| GET | `/api/csms/chargers/{id}` | One station: boot info, last status per connector / EVSE, transactions, recent meter values, counts and last payload per action, warnings, sent CALLs, plan state |
| GET | `/api/csms/transactions` | Transactions of every station |
| GET | `/api/csms/actions?version=` | CSMS → CP actions that can be sent for `1.6` / `2.0.1` |
| POST | `/api/csms/chargers/{id}/call` | Body `{"action", "payload", "timeout"?}`. Checked against the schema (400 naming the field), then sent. Answer `{"ok": true, "result"}` (plus `warning` if the answer does not match its schema) or `{"ok": false, "error": {"code", "description", "details"}}` for a CALLERROR |
| GET | `/api/csms/frames?since=&charger=&limit=` | The Mini CSMS's own frame log (direction seen from the CSMS) |

Errors: 404 unknown station, 400 schema mismatch, 409 station not connected, 504 no answer in time, 409 while the
Mini CSMS is not running (except `GET /api/csms`).

## WebSocket `/ws`

Clients that want live updates connect to `ws://127.0.0.1:8082/ws`:

1. The first message is `{"type": "snapshot", "data": ...}` with the same content as `GET /api/state`.
2. Then the engine pushes `charger_state`, `values` and `frame` messages, each with a `push_seq` counting
   1, 2, 3 ... per connection.
3. Engine events arrive as `{"type": "log", "event": {...}}`, numbered by their own `event.seq` (no `push_seq`).
4. After a configuration reload a new `{"type": "snapshot"}` is sent.

A gap in `push_seq` means messages were dropped for a slow client. If a state push or a reload notice was among
them, the next state push is a full one (`"full": true`), so the state heals by itself (or refetch
`/api/state`). Lost frames can be fetched with `GET /api/frames?since=<last frame seq seen>`. A full
`charger_state` lists *every* charger: replace your charger list with it (a missing charger was removed by a
reload); a full `values` replaces all values likewise. Do not rely on the order of the two full pushes.
