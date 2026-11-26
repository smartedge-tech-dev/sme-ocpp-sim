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
| GET | `/api/chargers/{id}/configuration` | Run-time configuration keys as GetConfiguration sees them |
| POST | `/api/devices` | Add a charger |
| GET / PUT | `/api/devices/{id}/config` | One charger's configuration (PUT replaces the whole item) |
| POST | `/api/devices/{id}/duplicate` | Copy a charger (the id is incremented) |
| DELETE | `/api/devices/{id}` | Remove a charger |

Passwords are always returned as `********`; sending the mask back keeps the stored password. Since a PUT
replaces the whole item, an omitted `password` means no password.

`/api/chargers/{id}/settings` covers `csms_url`, `ocpp_version`, `password`, `ca_file`, `verify_tls`,
`id_tags` and `config_keys`, plus the `effective` CSMS URL and OCPP version. PATCH changes only the fields
it contains; `null` unsets a field (`id_tags` / `config_keys` become empty; `verify_tls` must be true or
false). The password mask round-trips as above. Changes are applied with a configuration reload and are
written to the file only by `POST /api/config/save`.

## Charger operations

| Method | Path | Body |
|---|---|---|
| POST | `/api/chargers/{id}/connect` | — (starts the connection loop) |
| POST | `/api/chargers/{id}/disconnect` | — (stays disconnected until `connect`; transactions survive like a network loss) |
| POST | `/api/chargers/{id}/plug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/unplug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/swipe` | `{"connector": 1, "id_tag": "04A1B2C3"}` — `id_tag` is required |
| POST | `/api/chargers/{id}/start` | `{"connector": 1, "id_tag": "04A1B2C3"}` — plug in and swipe; `id_tag` defaults to the first configured tag (`400` when there is none); `400` when the connector already has a transaction or is faulted |
| POST | `/api/chargers/{id}/stop` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/fault` | `{"connector": 1, "error_code": "OtherError"}` — a 1.6 `ChargePointErrorCode`; ends a running transaction with reason `Other` and reports `Faulted` |
| POST | `/api/chargers/{id}/clear-fault` | `{"connector": 1}` — back to Available / Preparing |
| POST | `/api/chargers/{id}/datatransfer` | `{"vendor_id": "...", "message_id": "...", "data": "..."}` (charger → CSMS) |
| POST | `/api/chargers/bulk/connect` | `{"ids": ["CP-0001", "CP-0002"]}`, `["CP-0001"]` or `{"all": true}` |
| POST | `/api/chargers/bulk/disconnect` | same as above |

Bulk operations are not atomic: chargers are handled one after the other, and an error stops the request with
the earlier ones already changed.

## Smart Charging

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/chargers/{id}/profiles?connector=` | Installed charging profiles |
| GET | `/api/chargers/{id}/composite?connector=&duration=&unit=` | The GetCompositeSchedule answer computed locally (`connector` 0 = whole charge point; `evse` is accepted as an alias) |

## Frame log

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/frames?since=&until=&charger=&action=&dir=&limit=` | OCPP frames with `since < seq <= until`, filtered by charger, action and direction |
| GET | `/api/frames.jsonl?...` | The same as a streamed JSON Lines download (without `limit`: every matching frame in the buffer) |

`/api/frames` returns `{"frames": [...], "last_seq": n, "truncated": bool}`: at most the newest `limit`
matching frames (default 500, maximum 5000), oldest first. `truncated: true` means older matching frames were
left out; `last_seq` is the newest sequence number of the whole log (filters ignored).

The frame log contains OCPP frames only, never HTTP headers or passwords.

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
