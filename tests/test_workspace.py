import tomllib
from pathlib import Path

import research_engine
import research_engine_client
from research_engine.config import Settings

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.0.1"


def _project_version(path: Path) -> str:
    return tomllib.loads(path.read_text())["project"]["version"]


def test_client_package_version() -> None:
    assert research_engine_client.__version__ == VERSION


def test_service_package_version() -> None:
    assert research_engine.__version__ == VERSION


def test_pyproject_versions_match() -> None:
    for path in (
        ROOT / "pyproject.toml",
        ROOT / "packages" / "research_engine" / "pyproject.toml",
        ROOT / "packages" / "research_engine_client" / "pyproject.toml",
    ):
        assert _project_version(path) == VERSION, path


def test_user_agent_keeps_major_minor_only() -> None:
    """The fetch User-Agent names the API generation, not the patch release."""
    ua = Settings.model_fields["user_agent"].default
    assert ua.startswith("ResearchEngine/1.0 ")
