"""Command line entry point.

  sme-ocpp-sim [run] [-c CONFIG] [--host 127.0.0.1] [--http-port 8082] [--no-browser]
               [-v] [--log-format {text,json}] [--mini-csms] [--csms-host HOST] [--csms-port 9000]
  sme-ocpp-sim scenario run FILE [-c CONFIG] [--out verdict.json] [--junit junit.xml] [--trace frames.jsonl]
                            [--time-scale N] [-v] [--log-format {text,json}]
  sme-ocpp-sim scenario validate FILE [-c CONFIG]
  sme-ocpp-sim --version

The common options and the web server start come from ``sme_sim_common.cli``. ``--mini-csms`` / ``--csms-host`` /
``--csms-port`` override ``mini_csms.enabled`` / ``host`` / ``port`` of the config for this run (saving the config
from the web UI then writes them, like any other running value).

``scenario run`` is headless (no web server): it starts the engine with the config, runs the scenario, writes the
verdict JSON (``--out``, default stdout) and exits 0 pass / 1 fail / 2 error, aborted or a load error (v0.3 M3).
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


def _load_config(path: Optional[str]):
    from .config import SimConfig, load_file

    if path is None:
        default = default_config_path()
        path = str(default) if default else None
    return (load_file(path) if path else SimConfig()), path


def _on_sigint(loop, abort):
    """Make Ctrl+C abort the scenario (the verdict is still written); returns the undo function. Windows has no
    ``loop.add_signal_handler``: a ``signal.signal`` handler hands the abort to the loop thread."""
    import signal

    try:
        loop.add_signal_handler(signal.SIGINT, abort)
        return lambda: loop.remove_signal_handler(signal.SIGINT)
    except (NotImplementedError, RuntimeError, ValueError):
        pass
    try:
        old = signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(abort))
    except ValueError:  # not the main thread
        return lambda: None
    return lambda: signal.signal(signal.SIGINT, old)


def scenario_main(argv) -> int:
    """``scenario run`` / ``scenario validate`` (exit codes: :mod:`.scenario.verdict`)."""
    from .scenario import builtin

    ap = argparse.ArgumentParser(prog=f"{APP_NAME} scenario", description="Run or check a scenario file "
                                 "(format ocppsim-scenario/1) against a simulator config, without the web UI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "validate"):
        p = sub.add_parser(name, help="run the scenario headless" if name == "run" else "check the scenario only")
        p.add_argument("file", help="scenario YAML, or builtin:<name> for a built-in scenario "
                       f"({', '.join(e['name'] for e in builtin.listing()) or 'none'})")
        p.add_argument("-c", "--config", help="simulator config YAML (default: as the web app)")
        if name == "run":
            p.add_argument("--out", help="verdict JSON file (default: stdout)")
            p.add_argument("--junit", help="also write a JUnit XML report")
            p.add_argument("--trace", help="write the frames of the scenario's chargers (JSON lines)")
            p.add_argument("--time-scale", dest="time_scale", type=float,
                           help="sim speed for this run (overrides the config and the scenario's clock.time_scale)")
            p.add_argument("-v", "--verbose", action="store_true")
            p.add_argument("--log-format", dest="log_format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        setup_logging(verbose=args.verbose, log_format=args.log_format)

    from .scenario import verdict as vd
    from .scenario.model import ScenarioError, expand, load_file as load_scenario, load_text

    try:
        cfg, _path = _load_config(args.config)
        if args.file.startswith(builtin.PREFIX):
            sc = load_text(builtin.text(args.file))
        else:
            sc = load_scenario(args.file)
        if getattr(args, "time_scale", None) is not None:
            if args.time_scale <= 0:
                raise ScenarioError("--time-scale: must be > 0")
            sc = sc.model_copy(update={"clock": sc.clock.model_copy(update={"time_scale": args.time_scale})})
        expand(sc, [c.id for c in cfg.chargers])
    except (ScenarioError, OSError) as e:
        print(f"scenario error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - config errors
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.cmd == "validate":
        print(f"{args.file}: ok ({len(sc.tracks)} track(s))")
        return 0

    import asyncio

    from .engine import OcppEngine
    from .scenario.runner import ScenarioRunner

    async def run() -> dict:
        engine = OcppEngine(cfg)
        runner = engine.scenario = ScenarioRunner(engine, sc, source=args.file)
        runner.trace_path = args.trace
        restore = _on_sigint(asyncio.get_running_loop(), runner.abort)
        try:
            await engine.start()
            runner.start()
            return await runner.wait()
        finally:
            restore()
            await engine.stop()

    try:
        verdict = asyncio.run(run())
        text = vd.to_json(verdict)
        if args.out:
            vd.write(args.out, text)
        else:
            sys.stdout.write(text)
        if args.junit:
            vd.write(args.junit, vd.to_junit(verdict))
    except KeyboardInterrupt:
        print("scenario aborted", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - engine start, run or writing the outputs
        print(f"scenario error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    failed = [s for s in verdict["steps"] if s["status"] in ("fail", "error")]
    print(f"scenario {verdict['scenario']}: {verdict['result']} ({len(verdict['steps'])} step(s), "
          f"{verdict['wall_s']:.1f} s wall, {verdict['sim_s']:.0f} s sim)"
          + "".join(f"\n  {s['track']} {s['id']} {s['status']}: {s['message']}" for s in failed)
          + (f"\n  error: {verdict['error']}" if verdict.get("error") else ""), file=sys.stderr)
    return vd.exit_code(verdict["result"])


def main(argv=None) -> int:
    rest = sys.argv[1:] if argv is None else list(argv)
    if rest[:1] == ["scenario"]:
        return scenario_main(rest[1:])
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
