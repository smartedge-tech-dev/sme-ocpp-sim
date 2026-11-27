# Development

```powershell
git clone https://github.com/smartedge-tech-dev/sme-ocpp-sim
cd sme-ocpp-sim
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\sme-ocpp-sim -c configs/demo-site.yaml
```

The web UI has no build step: `sme_ocpp_sim/static/` is served as is, on top of the shared shell of
`sme-sim-common`.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

The integration tests run the simulated chargers against an in-process OCPP 1.6 peer (the `ocpp` library, a
dev dependency only). The sme-sim-common pytest plugin limits every `asyncio.run` to `SIM_TEST_TIMEOUT`
seconds (default 60); raise it on a slow machine, e.g. `$env:SIM_TEST_TIMEOUT = "180"`.

### Load test

The load test is excluded by default (marker `load`):

```powershell
.venv\Scripts\python -m pytest -m load -s
```

| Variable | Default | Meaning |
|---|---|---|
| `SME_LOAD_CHARGERS` | `200` | Number of chargers |
| `SME_LOAD_SECONDS` | `600` | Duration in seconds |
| `SME_LOAD_HEARTBEAT_S` | `10` | Heartbeat interval the peer returns in BootNotification |
| `SME_LOAD_METER_S` | `60` | MeterValueSampleInterval; every charger runs one transaction |

The load test raises its own time limit to `SME_LOAD_SECONDS` + 300 s.

## Opening dialogs from scripts

For screenshots and UI tests, the main dialogs can be opened with global functions in the browser:

| Function | Opens |
|---|---|
| `window.openChargerDialog(id)` | Selects the charger and scrolls to its details (no dialog) |
| `window.openSettingsDialog(id?)` | The settings dialog (CSMS defaults, plus the connection settings of charger `id`, default: the selected one) |
| `window.openProfileDialog(id)` | The Smart Charging profiles of a charger |
| `window.openFrameDetail(seq)` | One frame of the frame log |
| `window.openAddChargerDialog()` | The "add charger" dialog |

Language and theme are stored in `localStorage` under `sme-ocpp-sim.lang` and `sme-ocpp-sim.theme`.

## Screenshots

The README and documentation images are made by `scripts/screenshots.py` (dev dependencies: Playwright, Pillow and
the `ocpp` library):

```powershell
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m playwright install chromium   # once
.venv\Scripts\python scripts\screenshots.py
```

The script works in a temporary directory: it starts the dev CSMS peers `tests/peers/ocpp16_peer.py` and
`tests/peers/ocpp201_peer.py` in-process, runs the simulator with `scripts/screenshots.yaml` (8 chargers, three of
them OCPP 2.0.1, staggered `initial_soc`, `time_scale` 60) on a free
port of `127.0.0.1`, starts a transaction on every charger and sends a TxDefaultProfile and an external limit to the 2.0.1 station `CP-0001`. It then
captures every language in the light theme plus a dark overview and writes `overview.png`, `charger.png`,
`smart-charging.png`, `frames.png`, `variables.png`, `settings.png`, `overview-dark.png` and `live.gif` to
`docs/assets/screenshots/{en,ja,zh}/` (and the wide `ja/overview-wide.png`). Language and theme are set through the
`localStorage` keys above.

## Documentation

```powershell
.venv\Scripts\pip install -r requirements-docs.txt
.venv\Scripts\mkdocs serve
```

Pages live in `docs/en`, `docs/ja` and `docs/zh` with the same file names. CI builds with
`mkdocs build --strict`.
