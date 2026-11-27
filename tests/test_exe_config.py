"""Default config location of the Windows executable (#27).

A PyInstaller onefile exe runs from a temporary extraction directory (``sys._MEIPASS``) that is deleted on exit,
so started without ``-c`` it uses the per-user ``~/.sme-ocpp-sim/config.yaml`` (created from the bundled demo
config on the first run) and never writes below ``sys._MEIPASS``. Frozen mode is faked here."""

import asyncio
import logging
import shutil
import sys
from pathlib import Path

import pytest

import sme_ocpp_sim.__main__ as cli
import sme_ocpp_sim.web as web
from sme_ocpp_sim import templates_builtin

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "configs" / "demo-site.yaml"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.delenv("SME_OCPP_SIM_TEMPLATES", raising=False)
    return h


@pytest.fixture
def frozen(tmp_path, monkeypatch, home):
    """Fake PyInstaller onefile: ``sys.frozen``, ``sys._MEIPASS`` and the bundled demo config inside it."""
    meipass = tmp_path / "_MEI12345"
    (meipass / "configs").mkdir(parents=True)
    bundled = meipass / "configs" / "demo-site.yaml"
    shutil.copyfile(DEMO, bundled)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", bundled)
    return meipass


def _start(monkeypatch, argv):
    """Run ``main`` without the web server; return the engine and the config path given to the web app."""
    seen = {}
    monkeypatch.setattr(web, "create_app", lambda engine, path: seen.update(engine=engine, path=path) or "app")
    monkeypatch.setattr(cli, "run_web", lambda app, args, name: None)
    assert cli.main(list(argv) + ["--no-browser"]) == 0
    return seen["engine"], seen["path"]


def _snapshot(d: Path):
    return {p.relative_to(d): p.read_bytes() for p in d.rglob("*") if p.is_file()}


def test_user_data_dir_is_shared_with_templates(home):
    assert templates_builtin.user_data_dir() == home / ".sme-ocpp-sim"
    assert templates_builtin.registry().default_templates_dir() == home / ".sme-ocpp-sim" / "templates"


def test_first_run_copies_bundled_config(frozen, home, monkeypatch, caplog):
    user = home / ".sme-ocpp-sim" / "config.yaml"
    assert not user.exists()
    with caplog.at_level(logging.INFO, logger="sme-ocpp-sim"):
        engine, path = _start(monkeypatch, [])
    assert path == str(user)
    assert user.read_bytes() == DEMO.read_bytes()
    assert engine.config_path == user and engine.config.chargers
    assert f"config file: {user}" in caplog.text


def test_second_run_reuses_user_config(frozen, home, monkeypatch):
    user = home / ".sme-ocpp-sim" / "config.yaml"
    _start(monkeypatch, [])
    edited = user.read_text(encoding="utf-8") + "\n# my edit\n"
    user.write_text(edited, encoding="utf-8")
    _, path = _start(monkeypatch, [])
    assert path == str(user)
    assert user.read_text(encoding="utf-8") == edited, "an existing user config is not overwritten"


def test_save_writes_user_file_never_meipass(frozen, home, monkeypatch):
    before = _snapshot(frozen)
    engine, _ = _start(monkeypatch, [])
    user = home / ".sme-ocpp-sim" / "config.yaml"
    user.write_text("", encoding="utf-8")  # the save must write the running config here
    saved = asyncio.run(engine.save_config())
    assert Path(saved) == user
    assert "chargers:" in user.read_text(encoding="utf-8")
    assert _snapshot(frozen) == before, "nothing may be written below sys._MEIPASS"


def test_explicit_config_used_in_place(frozen, home, monkeypatch, tmp_path):
    mine = tmp_path / "my-site.yaml"
    shutil.copyfile(DEMO, mine)
    engine, path = _start(monkeypatch, ["-c", str(mine)])
    assert path == str(mine)
    assert Path(asyncio.run(engine.save_config())) == mine
    assert not (home / ".sme-ocpp-sim").exists(), "no user config is created when -c is given"


def test_not_frozen_uses_bundled_demo(home, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    _, path = _start(monkeypatch, [])
    assert path == str(cli.DEFAULT_CONFIG) == str(DEMO)
    assert not (home / ".sme-ocpp-sim").exists()
