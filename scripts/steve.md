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
