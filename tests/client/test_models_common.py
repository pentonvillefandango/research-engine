import pytest
from pydantic import ValidationError
from research_engine_client.models.common import (
    SCHEMA_VERSION,
    Envelope,
    ErrorCode,
    ErrorDetail,
    Meta,
)


def test_schema_version_is_semver() -> None:
    assert SCHEMA_VERSION == "1.0.0"


def test_meta_defaults() -> None:
    meta = Meta(request_id="r1", took_ms=5)
    assert meta.schema_version == SCHEMA_VERSION
    assert meta.cache_hit is False


def test_envelope_roundtrip_generic() -> None:
    env = Envelope[int](data=1, meta=Meta(request_id="r1", took_ms=1))
    again = Envelope[int].model_validate_json(env.model_dump_json())
    assert again == env


def test_envelope_always_serialises_all_keys() -> None:
    env = Envelope[int](
        data=None,
        meta=Meta(request_id="r", took_ms=0),
        errors=[ErrorDetail(code=ErrorCode.NOT_FOUND, message="x", retryable=False)],
    )
    dumped = env.model_dump(mode="json")
    assert set(dumped) == {"data", "meta", "errors"}
    assert dumped["data"] is None
    assert dumped["errors"][0] == {
        "code": "not_found",
        "message": "x",
        "retryable": False,
        "source": None,
    }


def test_error_code_is_enum() -> None:
    with pytest.raises(ValidationError):
        ErrorDetail(code="bogus", message="x", retryable=False)  # type: ignore[arg-type]


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Meta(request_id="r", took_ms=1, surprise=True)  # type: ignore[call-arg]
