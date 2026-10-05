"""`make smoke`: the smoke CLI run inside the app container (V1-22, V1-21)."""

from .conftest import FakeEnv
from .test_readonly_ops import json_lines

# ---- smoke -----------------------------------------------------------------------------------


def test_smoke_passes_through_cli_line(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke")
    assert r.code == 0, r.stderr
    assert len(json_lines(r.stdout)) == 1
    assert r.last["ok"] is True and r.last["command"] == "smoke"
    assert r.last["checks"][1] == {"detail": "12 results", "ms": 900, "name": "search", "ok": True}
    calls = fake_env.calls.read_text()
    assert "-p research-engine " in calls
    assert "exec -T app research-engine smoke --url http://127.0.0.1:8000" in calls
    for secret in fake_env.secrets:
        assert secret not in r.stdout + r.stderr + calls


def test_smoke_failure_exits_1_with_cli_line(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke", FAKE_SMOKE_FILE="exec_smoke_fail.json", FAKE_SMOKE_EXIT="1")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1
    assert r.last["ok"] is False and r.last["checks"][0]["name"] == "mcp"


def test_smoke_ok_line_but_nonzero_exit_fails(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke", FAKE_SMOKE_EXIT="1")
    assert r.code == 1 and r.last["ok"] is False and r.last["command"] == "smoke"


def test_smoke_no_json_from_container_fails(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke", FAKE_SMOKE_FILE="exec_smoke_garbage.txt", FAKE_SMOKE_EXIT="1")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1
    assert r.last["ok"] is False and r.last["command"] == "smoke" and r.last["error"]
    assert "Traceback" not in r.stdout


def test_smoke_docker_failure_one_json_line(fake_env: FakeEnv) -> None:
    r = fake_env.run("smoke", FAKE_FAIL="125")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1 and r.last["ok"] is False


def test_smoke_result_stays_valid_json_when_a_detail_is_redacted(fake_env: FakeEnv) -> None:
    """Redaction runs on the parsed JSON's string values, never on the serialised line, so a
    ``name: value``-shaped detail can't swallow the closing quotes (v1.0.1 review finding 2)."""
    r = fake_env.run("smoke", FAKE_SMOKE_FILE="exec_smoke_colon_detail.json", FAKE_SMOKE_EXIT="1")
    assert r.code == 1 and len(json_lines(r.stdout)) == 1
    assert r.last["ok"] is False and r.last["command"] == "smoke" and "error" not in r.last
    checks = r.last["checks"]
    assert checks[0] == {
        "detail": "ValueError: invalid key: ***REDACTED***",
        "ms": 5,
        "name": "fetch",
        "ok": False,
    }
    assert checks[1]["detail"] == "upstream said token=***REDACTED***"
    assert checks[2]["api_key"] == "***REDACTED***"
    assert "SEKRIT" not in r.stdout + r.stderr
