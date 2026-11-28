# MCP, health check and logs

## MCP endpoint

SME OCPPSim serves an [MCP](https://modelcontextprotocol.io/) endpoint on its web port, so an AI assistant
(Claude Code, Claude Desktop, other MCP clients) can drive the simulated chargers: list them, plug in, start and
stop transactions, inject faults and network impairments, read the frame log.

- URL: `http://127.0.0.1:8082/mcp` (MCP streamable HTTP, stateless, JSON responses; same port as the web UI)
- No authentication, the same as the web UI and the [HTTP API](http-api.md). With the default `--host 127.0.0.1`
  only requests with a loopback `Host` / `Origin` header are accepted (DNS-rebinding protection); with
  `--host 0.0.0.0` (Docker, LAN) the endpoint is open to everyone who can reach the port.

Claude Code:

The DNS-rebinding protection applies to `/mcp` only: the rest of the HTTP API, like the web UI, has
none, so bind to loopback or a trusted network.

```bash
claude mcp add --transport http ocpp http://127.0.0.1:8082/mcp
```

Clients that only speak stdio (e.g. Claude Desktop) can use a bridge such as
`npx mcp-remote http://127.0.0.1:8082/mcp`.

Every tool calls one [HTTP API](http-api.md) route in-process: same validation, same errors (an HTTP error
becomes a tool error with its message), same effect as the button in the web UI. Charger tools take the charger
id as `cid`; connector numbers start at 1.

| Tool | HTTP route | Meaning |
|---|---|---|
| `get_version` | `GET /api/version` | Product and library versions |
| `get_state` | `GET /api/state` | All chargers with their state (the snapshot the web UI shows) |
| `export_config` | `GET /api/config` | The running configuration as YAML (passwords masked) |
| `set_simulation` | `POST /api/simulation` | Run / pause, time scale |
| `get_charger` | `GET /api/chargers/{id}` | One charger, its fault codes and CSMS actions |
| `get_charger_settings` | `GET /api/chargers/{id}/settings` | Connection / authorization settings |
| `get_charging_profiles` | `GET /api/chargers/{id}/profiles` | Installed charging profiles |
| `get_composite_schedule` | `GET /api/chargers/{id}/composite` | Composite schedule, computed locally |
| `get_frames` | `GET /api/frames` | Frame log entries (filters `charger`, `action`, `dir`) |
| `get_impairments` | `GET /api/chargers/{id}/impairments` | Fault injection settings |
| `connect_charger` / `disconnect_charger` | `POST /api/chargers/{id}/connect` / `disconnect` | Connection loop on / off |
| `plug_in` / `unplug` | `POST /api/chargers/{id}/plug` / `unplug` | EV cable |
| `authorize` | `POST /api/chargers/{id}/swipe` | Present an id tag (start / stop) |
| `start_transaction` / `stop_transaction` | `POST /api/chargers/{id}/start` / `stop` | Plug in + id tag / local stop |
| `data_transfer` | `POST /api/chargers/{id}/datatransfer` | Send a DataTransfer to the CSMS |
| `set_fault` / `clear_fault` | `POST /api/chargers/{id}/fault` / `clear-fault` | Connector fault |
| `set_external_limit` | `POST /api/chargers/{id}/external-limit` | External charging limit (2.0.1) |
| `set_impairments` | `PUT /api/chargers/{id}/impairments` | Command blocking, latency, periodic drops, remote start / meter value / slow response anomalies |
| `drop_connection` | `POST /api/chargers/{id}/drop` | Drop the connection now |
| `send_frame` | `POST /api/chargers/{id}/send` | Inject a CALL or a raw frame (expert use) |
| `mini_csms_status` | `GET /api/csms` | Built-in [Mini CSMS](mini-csms.md) status |
| `mini_csms_actions` | `GET /api/csms/actions` | CSMS → charger actions the Mini CSMS can send |
| `mini_csms_call` | `POST /api/csms/chargers/{id}/call` | Send a CSMS → charger CALL from the Mini CSMS |
| `run_scenario` | `POST /api/scenarios/run` | Start a [scenario](scenarios.md) (argument `body`: the YAML text, or `builtin:<name>`; an unknown built-in is a 404 error, invalid YAML a 400 error naming the problem) |
| `get_scenario_status` | `GET /api/scenarios/current` | Status and progress of the scenario |
| `get_scenario_result` | `GET /api/scenarios/result` | Verdict of the finished scenario |
| `abort_scenario` | `POST /api/scenarios/abort` | Stop the running scenario |
| `list_builtin_scenarios` | `GET /api/scenarios/builtin` | The built-in scenarios (name, title, description) |
| `get_traffic` | `GET /api/traffic` | Standing [traffic](traffic.md): state, counters, power |
| `set_traffic` | `PUT /api/traffic` | Replace the `traffic:` block |
| `start_traffic` / `stop_traffic` | `POST /api/traffic/start` / `stop` | Start / stop the standing traffic |
| `get_trace` / `start_trace` / `stop_trace` | `GET /api/trace`, `POST /api/trace/start` / `stop` | [Trace](trace-replay.md) recording |
| `analyze_frames` | `GET /api/frames/analyze` | Analysis of the frame log |
| `preview_traffic_plan` | `GET /api/traffic/plan` | The planned arrivals (deterministic for a seed) |
| `expect_frame` | `POST /api/frames/expect` | Wait for a frame matching filters and payload matchers |

The charger action tools (`plug_in`, `start_charging`, `send_frame` …) return `last_seq`, the frame-log seq before
the action: pass it as `since` to `expect_frame` so the frames the action caused are not missed. Error results carry no `last_seq`; the bulk tools return one for the whole batch.

Read-only tools are marked `readOnlyHint`; `disconnect_charger`, `drop_connection`, `set_impairments`, `send_frame` and
`mini_csms_call` are marked `destructiveHint`, so a client can ask before calling them.

## Health check

`GET /healthz` answers `200` with `{"status": "ok", "name", "version", "common_version"}` while the web server
runs, for Docker / Kubernetes probes:

```bash
curl -fsS http://127.0.0.1:8082/healthz
```

## JSON logs

`--log-format json` writes one JSON object per log line (`ts`, `level`, `logger`, `message`, extra fields,
`exc`) instead of text, for log collectors; the web server's own logs use the same format. The default is
`--log-format text`. `-v` / `--verbose` switches to debug level in both formats.

```bash
sme-ocpp-sim --log-format json
```
