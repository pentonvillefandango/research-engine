import json
import os
import stat
from pathlib import Path

import pytest
from research_engine import cli
from research_engine.keys.envfile import set_env_value
from research_engine.keys.password import verify_password


def _env(tmp_path: Path, body: str, name: str = ".env") -> Path:
    p = tmp_path / name
    p.write_text(body)
    p.chmod(0o600)
    return p


def test_set_env_value_replaces_and_keeps_mode(tmp_path: Path) -> None:
    p = _env(tmp_path, "A=1\nGUI_PASSWORD_HASH=old\nB=2\n")
    set_env_value(p, "GUI_PASSWORD_HASH", "new")
    assert p.read_text() == "A=1\nGUI_PASSWORD_HASH=new\nB=2\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_set_env_value_uncomments_or_appends(tmp_path: Path) -> None:
    p = _env(tmp_path, "A=1\n# GUI_PASSWORD_HASH=\n")
    set_env_value(p, "GUI_PASSWORD_HASH", "h")
    assert p.read_text() == "A=1\nGUI_PASSWORD_HASH=h\n"
    q = _env(tmp_path, "A=1", "other.env")  # no trailing newline
    set_env_value(q, "GUI_PASSWORD_HASH", "h")
    assert q.read_text() == "A=1\nGUI_PASSWORD_HASH=h\n"


def test_set_env_value_replaces_empty_line(tmp_path: Path) -> None:
    p = _env(tmp_path, "A=1\nGUI_PASSWORD_HASH=\nB=2\n")
    set_env_value(p, "GUI_PASSWORD_HASH", "h")
    assert p.read_text() == "A=1\nGUI_PASSWORD_HASH=h\nB=2\n"


def _prompts(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    it = iter(answers)
    monkeypatch.setattr(cli, "getpass", lambda prompt="": next(it))


def test_cli_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _env(tmp_path, "API_KEY=x\n")
    _prompts(monkeypatch, "correct horse battery", "correct horse battery")
    assert cli.main(["gui", "set-password", "--env-file", str(p)]) == 0
    out = capsys.readouterr().out
    line = json.loads(out.strip().splitlines()[-1])
    assert line["ok"] is True and line["command"] == "gui set-password"
    assert line["next"] == "make deploy"
    stored = next(x for x in p.read_text().splitlines() if x.startswith("GUI_PASSWORD_HASH="))
    h = stored.split("=", 1)[1]
    assert verify_password("correct horse battery", h)
    assert h not in out and "correct horse battery" not in out


@pytest.mark.parametrize(
    "answers", [("correct horse battery", "different horse"), ("short", "short")]
)
def test_cli_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: tuple[str, str]
) -> None:
    p = _env(tmp_path, "API_KEY=x\n")
    _prompts(monkeypatch, *answers)
    assert cli.main(["gui", "set-password", "--env-file", str(p)]) == 2
    assert p.read_text() == "API_KEY=x\n"


def test_cli_missing_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _prompts(monkeypatch, "correct horse battery", "correct horse battery")
    assert cli.main(["gui", "set-password", "--env-file", str(tmp_path / "nope")]) == 2
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ok"] is False
