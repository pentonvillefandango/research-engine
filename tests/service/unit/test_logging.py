import io
import json
import sys

import pytest
import structlog
from research_engine.logging import configure_logging


def test_logs_follow_the_current_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured while one stream is installed, logging must still follow later swaps."""
    first, second = io.StringIO(), io.StringIO()
    try:
        monkeypatch.setattr(sys, "stdout", first)
        configure_logging("INFO")
        structlog.get_logger("t").info("one")
        monkeypatch.setattr(sys, "stdout", second)
        structlog.get_logger("t").info("two")
        configure_logging("INFO")  # reconfiguring must not pin the old stream either
        monkeypatch.setattr(sys, "stdout", first)
        structlog.get_logger("t").info("three")
    finally:
        monkeypatch.undo()
        structlog.reset_defaults()
    assert [json.loads(x)["event"] for x in first.getvalue().splitlines()] == ["one", "three"]
    assert [json.loads(x)["event"] for x in second.getvalue().splitlines()] == ["two"]
