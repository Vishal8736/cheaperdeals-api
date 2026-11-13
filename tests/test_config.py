from __future__ import annotations

import os

from app.config import load_env_file


def test_env_file_parsing(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nFOO_A=plain\nFOO_B='quoted $value'\nFOO_C=\"dq\"\nFOO_D=val # inline\nFOO_E=keep\n\nbad line\n")
    for k in "ABCDE":
        monkeypatch.delenv(f"FOO_{k}", raising=False)
    monkeypatch.setenv("FOO_E", "from-real-env")
    assert load_env_file(f) is True
    got = {k: os.environ[f"FOO_{k}"] for k in "ABCDE"}
    assert got == {"A": "plain", "B": "quoted $value", "C": "dq", "D": "val", "E": "from-real-env"}
    for k in "ABCD":
        monkeypatch.delenv(f"FOO_{k}", raising=False)


def test_missing_env_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / "nope.env") is False


def test_unknown_timezone_falls_back_to_utc():
    from datetime import timezone
    from app.config import Config
    assert Config(timezone="Not/AZone").tzinfo is timezone.utc
    assert str(Config(timezone="Asia/Kolkata").tzinfo) in {"Asia/Kolkata"}
