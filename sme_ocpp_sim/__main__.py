"""Command line entry point.

  sme-ocpp-sim [run] [-c CONFIG] [--host 127.0.0.1] [--http-port 8082] [--no-browser]
               [--mini-csms] [--csms-host HOST] [--csms-port 9000]
  sme-ocpp-sim --version

The common options and the web server start come from ``sme_sim_common.cli``. ``--mini-csms`` / ``--csms-host`` /
``--csms-port`` override ``mini_csms.enabled`` / ``host`` / ``port`` of the config for this run (saving the config
from the web UI then writes them, like any other running value).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sme_sim_common.cli import add_common_args, run_web

from . import __version__

APP_NAME = "sme-ocpp-sim"
DEFAULT_HTTP_PORT = 8082  # series port table (sme-sim-common README)
DEFAULT_CSMS_PORT = 9000  # Mini CSMS WebSocket port (plan §11 Q3)
_PKG = Path(__file__).resolve().parent
# Default config when started without -c (as sme-modbus-sim / sme-bac-sim).
# Installed wheel: sme_ocpp_sim/configs; source tree, Docker image and Windows exe (PyInstaller puts the
# package under sys._MEIPASS, so __file__ resolves there): configs/ beside the package.
DEFAULT_CONFIG = next((p for p in (_PKG / "configs" / "demo-site.yaml", _PKG.parent / "configs" / "demo-site.yaml")
                       if p.exists()), _PKG.parent / "configs" / "demo-site.yaml")


def main(argv=None) -> int:
    rest = sys.argv[1:] if argv is None else list(argv)
    if rest[:1] == ["run"]:
        rest = rest[1:]
    ap = argparse.ArgumentParser(prog=APP_NAME,
                                 description="OCPP charging station simulator with a web UI; "
                                             "default config: the bundled configs/demo-site.yaml")
    add_common_args(ap, default_http_port=DEFAULT_HTTP_PORT, app_name=APP_NAME, version=__version__,
                    templates=False)
    g = ap.add_argument_group("Mini CSMS (built-in simple CSMS, overrides the mini_csms block)")
    g.add_argument("--mini-csms", dest="mini_csms", action="store_true", default=None,
                   help="enable the Mini CSMS WebSocket server")
    g.add_argument("--csms-host", dest="csms_host", help="Mini CSMS listen host (use 0.0.0.0 in Docker)")
    g.add_argument("--csms-port", dest="csms_port", type=int,
                   help=f"Mini CSMS listen port (default {DEFAULT_CSMS_PORT})")
    args = ap.parse_args(rest)

    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .config import SimConfig, load_file
    from .engine import OcppEngine

    path = args.config
    if path is None and DEFAULT_CONFIG.exists():
        path = str(DEFAULT_CONFIG)
    try:
        cfg = load_file(path) if path else SimConfig()
        override = {k: v for k, v in (("enabled", args.mini_csms), ("host", args.csms_host),
                                      ("port", args.csms_port)) if v is not None}
        if override:
            cfg = SimConfig.model_validate({**cfg.model_dump(), "mini_csms": {**cfg.mini_csms.model_dump(), **override}})
        text = Path(path).read_text(encoding="utf-8") if path else None
        engine = OcppEngine(cfg, config_text=text, config_path=path)
    except Exception as e:  # noqa: BLE001
        print(f"config error: {e}", file=sys.stderr)
        return 2

    from .web import create_app

    run_web(create_app(engine, path), args, name=f"SME OCPPSim v{__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
