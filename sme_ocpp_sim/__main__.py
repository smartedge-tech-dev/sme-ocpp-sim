"""Command line entry point.

  sme-ocpp-sim [run] [-c CONFIG] [--host 127.0.0.1] [--http-port 8082] [--no-browser]
               [-v] [--log-format {text,json}] [--mini-csms] [--csms-host HOST] [--csms-port 9000]
  sme-ocpp-sim --version

The common options and the web server start come from ``sme_sim_common.cli``. ``--mini-csms`` / ``--csms-host`` /
``--csms-port`` override ``mini_csms.enabled`` / ``host`` / ``port`` of the config for this run (saving the config
from the web UI then writes them, like any other running value).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import Optional

from sme_sim_common.cli import add_common_args, run_web
from sme_sim_common.logs import setup_logging

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
USER_CONFIG_NAME = "config.yaml"

log = logging.getLogger(APP_NAME)


def default_config_path() -> Optional[Path]:
    """Config file used when started without ``-c``.

    Windows executable (PyInstaller onefile, ``sys.frozen``): the bundled config lives in the temporary
    extraction directory (``sys._MEIPASS``) that is deleted when the exe exits, so saving from the web UI there
    would be lost (#27). The default is the per-user ``~/.sme-ocpp-sim/config.yaml`` instead; on the first run it
    is created as a copy of the bundled demo config. Nothing is ever written below ``sys._MEIPASS``.

    Everything else (pip, source tree, Docker): the bundled demo config itself, as before.
    """
    if not getattr(sys, "frozen", False):
        return DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None
    from .templates_builtin import user_data_dir

    user = user_data_dir() / USER_CONFIG_NAME
    if not user.exists() and DEFAULT_CONFIG.exists():
        user.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULT_CONFIG, user)
        log.info("created %s from the bundled %s", user, DEFAULT_CONFIG.name)
    return user if user.exists() else None


def main(argv=None) -> int:
    rest = sys.argv[1:] if argv is None else list(argv)
    if rest[:1] == ["run"]:
        rest = rest[1:]
    ap = argparse.ArgumentParser(prog=APP_NAME,
                                 description="OCPP charging station simulator with a web UI; "
                                             "default config: the bundled configs/demo-site.yaml "
                                             "(Windows exe: ~/.sme-ocpp-sim/config.yaml, created from it)")
    add_common_args(ap, default_http_port=DEFAULT_HTTP_PORT, app_name=APP_NAME, version=__version__,
                    templates=False, log_format=True)
    g = ap.add_argument_group("Mini CSMS (built-in simple CSMS, overrides the mini_csms block)")
    g.add_argument("--mini-csms", dest="mini_csms", action="store_true", default=None,
                   help="enable the Mini CSMS WebSocket server")
    g.add_argument("--csms-host", dest="csms_host", help="Mini CSMS listen host (use 0.0.0.0 in Docker)")
    g.add_argument("--csms-port", dest="csms_port", type=int,
                   help=f"Mini CSMS listen port (default {DEFAULT_CSMS_PORT})")
    args = ap.parse_args(rest)

    setup_logging(verbose=args.verbose, log_format=args.log_format)  # --log-format json: one object per line

    from .config import SimConfig, load_file
    from .engine import OcppEngine

    path = args.config
    try:
        if path is None:
            default = default_config_path()
            path = str(default) if default else None
        log.info("config file: %s", path or "(none, built-in defaults)")
        cfg = load_file(path) if path else SimConfig()
        override = {k: v for k, v in (("enabled", args.mini_csms), ("host", args.csms_host),
                                      ("port", args.csms_port)) if v is not None}
        if override:
            cfg = SimConfig.model_validate({**cfg.model_dump(), "mini_csms": {**cfg.mini_csms.model_dump(), **override}})
        text = Path(path).read_text(encoding="utf-8") if path else None
        engine = OcppEngine(cfg, config_text=text, config_path=path)
        if path:
            engine.log_event("config", f"config file: {path}")  # also in the web UI event log
    except Exception as e:  # noqa: BLE001
        print(f"config error: {e}", file=sys.stderr)
        return 2

    from .web import create_app

    run_web(create_app(engine, path, host=args.host), args, name=f"SME OCPPSim v{__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
