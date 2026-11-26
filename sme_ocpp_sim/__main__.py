"""Command line entry point.

  sme-ocpp-sim [run] [-c CONFIG] [--host 127.0.0.1] [--http-port 8082] [--no-browser]
  sme-ocpp-sim --version

The common options and the web server start come from ``sme_sim_common.cli``.
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


def main(argv=None) -> int:
    rest = sys.argv[1:] if argv is None else list(argv)
    if rest[:1] == ["run"]:
        rest = rest[1:]
    ap = argparse.ArgumentParser(prog=APP_NAME,
                                 description="OCPP charging station simulator with a web UI")
    add_common_args(ap, default_http_port=DEFAULT_HTTP_PORT, app_name=APP_NAME, version=__version__,
                    templates=False)
    args = ap.parse_args(rest)

    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .config import SimConfig, load_file
    from .engine import OcppEngine

    path = args.config
    try:
        cfg = load_file(path) if path else SimConfig()
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
