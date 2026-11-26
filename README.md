# SME OCPPSim

**English** | [日本語](README.ja.md) | [简体中文](README.zh-CN.md)

Free OCPP charging station simulator (OCPP 1.6J; 2.0.1 / 2.1 planned) for CSMS and EMS testing, including Smart Charging.

SME OCPPSim is free and MIT-licensed, developed by [smart-edge (智届科技)](https://smartedge-tech.com) —
custom development is available, see [Commercial support & custom development](#commercial-support--custom-development).

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\pip install sme-ocpp-sim
.venv\Scripts\sme-ocpp-sim --config configs/demo-site.yaml
```

Open http://127.0.0.1:8082/ . Common options (from sme-sim-common):

| Option | Meaning |
|---|---|
| `-c, --config FILE` | YAML config |
| `--host 127.0.0.1` | Web UI bind address (default localhost only) |
| `--http-port 8082` | Web UI port |
| `--no-browser` | Do not open the browser on start |
| `--version`, `-v` | Show the version / verbose logging |

A Windows single-file executable is attached to every [release](https://github.com/smartedge-tech-dev/sme-ocpp-sim/releases).

## Configuration

TODO

## HTTP API

TODO

## Development

```powershell
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
```

## Commercial support & custom development

TODO: copy this section from the SME BACnetSim README.

## License

MIT — see [LICENSE](LICENSE).
