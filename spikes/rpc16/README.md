# OCPP 1.6J RPC spike

- `rpc.py` - self-written OCPP-J RPC framer
- `cp.py` - minimal charge point; `csms_peer.py` - ocpp-lib based peer; `test_spike.py` - tests
- `steve_check.py` - interop check against a real CSMS (SteVe)

## SteVe interop check

SteVe (GPL-3.0) is used only as an external process; its checkout lives outside this repo.

```sh
# one-time: clone the latest release tag with LF line endings (mvnw runs inside Linux)
git -c core.autocrlf=false clone --depth 1 --branch steve-3.15.0 \
    https://github.com/steve-community/steve <steve-dir>

# start (first run builds with Maven inside the container, several minutes)
cd <steve-dir> && docker compose up -d
# wait until `docker compose logs app` shows the application started, then:
cd spikes/rpc16 && ../../.venv/Scripts/python steve_check.py --compose-dir <steve-dir>

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
