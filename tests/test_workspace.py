import research_engine
import research_engine_client


def test_client_package_version() -> None:
    assert research_engine_client.__version__ == "0.1.0"


def test_service_package_version() -> None:
    assert research_engine.__version__ == "0.1.0"
