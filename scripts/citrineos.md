# CitrineOS interop check (manual, OCPP 2.0.1)

`scripts/e2e_citrineos.py` runs the 2.0.1 check on the real `ChargerRuntime` (`ocpp_version "2.0.1"`) against
CitrineOS (Apache-2.0), the 2.0.1 counterpart of `scripts/e2e_steve.py` / `scripts/steve.md`.
CitrineOS is used only as an external process; its checkout lives outside this repo.

```sh
# one-time
git clone --depth 1 https://github.com/citrineos/citrineos-core <citrineos-dir>

# start (base stack only: citrine server, PostgreSQL/PostGIS, RabbitMQ, Hasura; no operator UI)
cd <citrineos-dir> && docker compose up -d
# up -d returns once the citrine container is healthy (TCP 8080 open), then:
.venv/Scripts/python scripts/e2e_citrineos.py --compose-dir <citrineos-dir> [--frames citrineos_frames.jsonl]

# stop (keeps containers, images and the bind-mounted data under apps/ocpp-server/data/)
cd <citrineos-dir> && docker compose stop
# restart later
cd <citrineos-dir> && docker compose start
```

Endpoints of the stack:
- OCPP-J, Security Profile 0: `ws://localhost:8081/<stationId>` (subprotocols `ocpp2.1`, `ocpp2.0.1`, `ocpp1.6`).
  SP1 (basic auth) is 8082, SP2 (TLS) 8443.
- REST (Swagger UI): http://localhost:8080/docs, spec at `/docs/json`. Message API e.g.
  `POST /ocpp/2.0.1/monitoring/getVariables?identifier=<stationId>&tenantId=1`.
- Hasura GraphQL console: http://localhost:8090.

Registration:
- Charging station: nothing to do. `apps/ocpp-server/src/assets/websocket-servers.json` sets
  `allowUnknownChargingStations: true` for the SP0 server, and `ocpp.unknownChargerStatus` defaults to
  `Accepted`; the station row (`ChargingStations`) is created at the first connect.
- Id token: the script inserts an `Accepted` row into `"Authorizations"` (`tenantId` 1, `idTokenType`
  `ISO14443`, `isPrepaid` false) with `docker compose exec ocpp-db psql -U citrine -d citrine`.
  Tokens not in that table are answered `idTokenInfo.status = Unknown`.

Pitfalls (CitrineOS v2.1.0, commit 4fd7599):
- `docker compose up -d` hung on pulling `ghcr.io/citrineos/citrineos-server:latest` (no layer progress for
  >10 min while the other three images finished). Killing it, `docker pull` of that image alone, then
  `docker compose up -d` again worked.
- The REST API needs `Authorization: Bearer <jwt>` (HTTP 401 otherwise). The stack ships a development endpoint
  `POST /dev/token` (body `{}`) that mints a token with the default local-dev roles; the script uses it.
- ISO14443 id tokens must be 8 or 14 hex digits (4 or 7 byte UID). Anything else, e.g. `SMETAG1`, is answered
  with a CALLERROR `PropertyConstraintViolation` ("ISO14443 tokens must be either 8 or 14 hexadecimal
  characters") instead of an `AuthorizeResponse`. The simulator then treats the token as not authorized. Use hex
  tokens (the script uses `5AE1C201` known / `0BADC0DE` unknown), or another `IdTokenEnumType`.
- `"Transactions"."stationId"` is the integer key of `"ChargingStations"` (`"ocppConnectionName"` holds the
  OCPP identity); `"TransactionEvents"` and `"MeterValues"` link via `"transactionDatabaseId"`.
- After an `Accepted` boot CitrineOS sends no CSMS calls on its own (`GetBaseReport` follows only a `Pending`
  boot, `ocpp.getBaseReportOnPending`). The boot interval is 60 s, so the first Heartbeat comes about a minute
  after boot.

Observations that are not simulator bugs (CitrineOS side):
- The ISO14443 format check above is stricter than the 2.0.1 JSON schema (`idToken` is any string up to 36
  characters). It follows the `IdTokenEnumType` description (4 or 7 byte UID), but a format error surfaces as a
  CALLERROR, not as `idTokenInfo.status = Invalid`. The simulator was not changed.
- The REST `getVariables` answer is `[{"success": true, "payload": "Batch [0]: undefined"}]`; the
  `GetVariablesResponse` itself is only visible in the frames / the `VariableAttributes` table.

## Run record

2026-11-27, CitrineOS `v2.1.0` (citrineos-core `4fd7599b91c2fb6e6065e7c9b94be69880cced27`, image
`ghcr.io/citrineos/citrineos-server:latest` built 2026-10-21). Images: citrineos-server 349 MB, postgis 16-3.5
634 MB, rabbitmq 3-management 252 MB, hasura graphql-engine v2.40.3 320 MB (~1.56 GB in total).

| Step | Result |
|---|---|
| WebSocket `ocpp2.0.1` on SP0, unknown station | connected, station row created |
| BootNotification | `Accepted`, interval 60 |
| StatusNotification (Available / Occupied / Available) | answered `{}` |
| Heartbeat | answered (after the 60 s interval) |
| CSMS calls after boot | none sent by CitrineOS (Accepted boot) |
| Unknown token `0BADC0DE` | Authorize -> `Unknown`, simulator refuses (no transaction) |
| Plug + known token `5AE1C201` | Authorize `Accepted` -> TransactionEvent `Started` (Authorized) |
| TransactionEvent `Updated` with meter values | `MeterValuePeriodic` every 5 s, rows in `"MeterValues"` |
| Local stop | TransactionEvent `Ended` (StopAuthorized), `stoppedReason` Local |
| REST GetVariables `OCPPCommCtrlr.HeartbeatInterval` | served, `Accepted` "60" |
| REST RequestStartTransaction (remoteStartId 4711) | `Accepted` -> `Started` (RemoteStart) |
| REST RequestStopTransaction | `Accepted` -> `Ended` (RemoteStop), `stoppedReason` Remote |
| DB check | `"Transactions"` rows inactive with totalKwh, `"TransactionEvents"` Started/Updated/Ended |
| CALLERRORs in either direction | none (with hex tokens) |

Not exercised: Security Profile 1/2, SetChargingProfile through REST, a `Pending` boot (GetBaseReport).
