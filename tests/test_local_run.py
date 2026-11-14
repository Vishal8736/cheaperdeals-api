"""Ctrl+C clean-up and the removal of simulated (demo) settings from an old local settings file."""
from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lr():
    spec = importlib.util.spec_from_file_location("local_run_mod", ROOT / "scripts" / "local_run.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_tidy_removes_only_generated_leftovers(tmp_path, monkeypatch):
    lr = _lr()
    root, dd = tmp_path / "proj", tmp_path / "data"
    (root / "api" / "app" / "__pycache__").mkdir(parents=True)
    (root / "api" / "app" / "__pycache__" / "x.pyc").write_text("x")
    (root / ".pytest_cache").mkdir(parents=True)
    (root / "frontend" / "node_modules" / "pkg" / "__pycache__").mkdir(parents=True)  # not ours: untouched
    (root / "api" / "app" / "main.py").write_text("code")
    (root / "api" / "logs").mkdir(parents=True)
    old, new, cur = root / "api/logs/cheaperdeals.log.3", root / "api/logs/cheaperdeals.log.1", root / "api/logs/cheaperdeals.log"
    for f in (old, new, cur):
        f.write_text("l")
    t = time.time() - 20 * 86400
    os.utime(old, (t, t))
    dd.mkdir()
    (dd / "cheapedeal.db").write_text("DATA")
    (dd / "cheapedeal.env").write_text("SECRET_KEY=abc")
    tmp = tmp_path / "tmp"
    (tmp / "cheaperdeals_site_abc" / "site").mkdir(parents=True)
    (tmp / "other_thing").mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp))
    n = lr.tidy(root, dd)
    assert n >= 4
    assert not (root / "api/app/__pycache__").exists() and not (root / ".pytest_cache").exists() and not (tmp / "cheaperdeals_site_abc").exists() and not old.exists()
    assert (root / "frontend/node_modules/pkg/__pycache__").exists() and (root / "api/app/main.py").read_text() == "code"
    assert new.exists() and cur.exists() and (dd / "cheapedeal.db").read_text() == "DATA" and (dd / "cheapedeal.env").exists() and (tmp / "other_thing").exists()


def test_goodbye_says_goodbye_and_never_raises(tmp_path, capsys, monkeypatch):
    lr = _lr()
    monkeypatch.setattr(lr, "tidy", lambda *a: (_ for _ in ()).throw(OSError("locked")))
    lr.goodbye(tmp_path, tmp_path)
    out = capsys.readouterr().out
    assert "Goodbye" in out and "safe hain" in out


def test_old_settings_file_loses_the_simulated_supplier_and_demo_tools(tmp_path):
    lr = _lr()
    p = tmp_path / "cheapedeal.env"
    p.write_text("APP_ENV=development\nSUPPLIER_BACKEND=mock\nSTORE_BACKEND=mock\nENABLE_DEV_TOOLS=true\nDRY_RUN=true\nADMIN_PASSWORD=abc\n")
    assert lr._drop_fake_settings(p) is True
    txt = p.read_text()
    assert "SUPPLIER_BACKEND" not in txt and "STORE_BACKEND" not in txt and "ENABLE_DEV_TOOLS=false" in txt and "ADMIN_PASSWORD=abc" in txt and "DRY_RUN=true" in txt
    assert lr._drop_fake_settings(p) is False
