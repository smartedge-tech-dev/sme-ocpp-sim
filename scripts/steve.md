# SteVe interop check (manual)

`scripts/e2e_steve.py` runs the check on the real `ChargerRuntime`. The original spike version is in git history:
`git show abc3067:spikes/rpc16/steve_check.py` (and `load_spike.py` for the 200-charger load check, M5).

SteVe (GPL-3.0) is used only as an external process; its checkout lives outside this repo.

```sh
# one-time: clone the latest release tag with LF line endings (mvnw runs inside Linux)
git -c core.autocrlf=false clone --depth 1 --branch steve-3.15.0 \
    https://github.com/steve-community/steve <steve-dir>

# start (first run builds with Maven inside the container, several minutes)
cd <steve-dir> && docker compose up -d
# wait until `docker compose logs app` shows the application started, then:
.venv/Scripts/python scripts/e2e_steve.py --compose-dir <steve-dir>

# stop (keeps containers, images and the db-data volume)
cd <steve-dir> && docker compose stop
# restart later
cd <steve-dir> && docker compose start
```

Web UI: http://localhost:8180/steve/manager (SteVe default test user `admin` / `1234`).
OCPP-J endpoint: `ws://localhost:8180/steve/websocket/CentralSystemService/<chargeBoxId>`.

Observed with SteVe 3.15.0 (first `up`: ~11 min incl. image pull + Maven build; the container CMD re-runs
`mvnw clean package` on every `start`, so restarts also rebuild, faster with the warm ~/.m2):
- `Sec-WebSocket-Protocol: ocpp2.0.1` only -> HTTP 404; no subprotocol header -> HTTP 400;
  unregistered chargeBoxId -> HTTP 404 at the handshake (no BootNotification exchange at all).
- SteVe sends `GetConfiguration {"key": []}` right after the WebSocket opens, before
  BootNotification has been answered; the CP must serve CSMS calls from the first frame.
- SteVe 3.15.0 can hang at startup in its release-check bean (`InternetChecker.isHostAvailable`
  blocks on an HTTPS read without a timeout). `docker compose restart app` recovers; readiness
  then took ~7 minutes.
- Charging profiles cannot be created by a plain form post to `/manager/chargingProfiles/add`
  (the controller needs the submit parameter and `schedulePeriods[i].*` added by JavaScript);
  for scripted runs insert into `charging_profile` / `charging_schedule_period` directly.

v0.1.0 run (2026-11-27, SteVe 3.15.0): Boot / Heartbeat / StatusNotification / Authorize (known + unknown) /
Start -> MeterValues -> Stop (rows in the SteVe DB) / GetConfiguration / ChangeConfiguration /
RemoteStart / Reset Soft / SetChargingProfile / GetCompositeSchedule / ClearChargingProfile all passed;
unregistered id and wrong subprotocol rejected at the handshake. Not exercised: RemoteStop
(no open transaction at that point), Security Profile 1 against SteVe.

v0.1.0 follow-up run (2026-11-28, SteVe 3.15.0, main @ 6228de9; issue #28): the two gaps above now pass.
- RemoteStopTransaction: a second transaction is started by swipe, then stopped from the web UI
  (`/manager/operations/v1.6/RemoteStopTransaction`, transactionId from the DB). The CP receives the call,
  sends StopTransaction, and SteVe stores `stop_event_actor=station`, `stop_reason=Remote`.
- Security Profile 1: the charge box is created through the web UI (`/manager/chargepoints/add/single`,
  securityProfile `Profile_1`, registrationStatus `ACCEPTED`, authPassword 16-20 chars, empty `address.*`
  fields - omitting them gives a NullPointerException page; SteVe stores a bcrypt hash). Right password ->
  online; wrong password -> HTTP 401, shown by the simulator as last_error `unauthorized`.
  The create step is not idempotent (a re-run fails on the duplicate id; the existing row keeps working).
- SteVe 3.15.0 schema notes for the script: `transaction` is a view over `transaction_start` / `transaction_stop`
  (open = `stop_value IS NULL`; stop an orphan with an INSERT into `transaction_stop`), transactions join
  `evse` (not `connector`). The script closes orphans of the test charge box at start, since an
  aborted run leaves a transaction that makes the next swipe answer ConcurrentTx.
- No simulator bug found. The script itself needed `rt.station.store` (was `rt.store`) for current main.

## Security Profile 3 (mTLS) through an nginx front

SteVe 3.15.0 knows the charge box setting `Profile_3`, but its Jetty connector does not request or verify TLS
client certificates (no `setNeedClientAuth` in the source), so it cannot check SP3 itself. The check therefore
uses a small nginx container (`nginx:stable-alpine3.24-slim`) in front of SteVe:
`scripts/steve-mtls-nginx.conf` terminates TLS, requires a client certificate chaining to the test root
(`ssl_verify_client on`), returns 403 unless the certificate CN equals the chargeBoxId in the URL, and proxies the
WebSocket to `app:8180` over plain HTTP.

**The 1.6 mTLS endpoint is the proxy, not SteVe:** `wss://localhost:8543/steve/websocket/CentralSystemService/<id>`.
SteVe sees an ordinary unauthenticated connection, so the charge box is registered with the default profile.

```sh
# 1. test PKI (shared with the CitrineOS check; keep it outside the repo, never commit the keys)
.venv/Scripts/python scripts/mtls_certs.py <cert-dir>

# 2. start the nginx front next to SteVe (host port 8543; SteVe and CitrineOS both publish 8443)
cd <steve-dir>
MTLS_CERT_DIR=<cert-dir, absolute> OCPPSIM_SCRIPTS=<repo>/scripts \
  docker compose -f docker-compose.yml -f <repo>/scripts/steve-mtls.compose.yml up -d --no-deps mtls-proxy

# 3. run only the SP3 check (charge box SME-SIM-3 and tag SMETAG1 are inserted into the DB)
.venv/Scripts/python scripts/e2e_steve.py --compose-dir <steve-dir> --profile3 <cert-dir>

# stop
MTLS_CERT_DIR=... OCPPSIM_SCRIPTS=... docker compose -f docker-compose.yml -f <repo>/scripts/steve-mtls.compose.yml stop
```

nginx answers a missing or untrusted client certificate with HTTP 400 after the TLS handshake (its internal 496 /
495 codes), not with a TLS alert, so the simulator shows `last_error` kind `bad_request` for both. A trusted
certificate whose CN is not the chargeBoxId in the URL gets HTTP 403.

Run record 2026-11-28 (SteVe 3.15.0, nginx stable-alpine3.24-slim, main @ da8b62d):

| Step | Result |
|---|---|
| Raw TLS probe of the proxy (Python `ssl`) | trusted cert -> proxied (502 while SteVe was still building); CN != chargeBoxId -> 403; no cert / untrusted CA -> 400 |
| BootNotification over mTLS (SME-SIM-3) | `Accepted`; the simulator reports SecurityProfile 3 |
| StatusNotification, Heartbeat | answered |
| Plug + swipe SMETAG1 -> StartTransaction -> MeterValues -> local stop -> StopTransaction | passed; SteVe row `stop_reason` Local |
| No client certificate / client certificate from an untrusted CA | refused (HTTP 400, `bad_request`), never online |
