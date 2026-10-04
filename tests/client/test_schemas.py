import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import BaseModel, ValidationError
from research_engine_client.models import (
    ALL_MODELS,
    Envelope,
    ErrorCode,
    ErrorDetail,
    Event,
    Meta,
    SearchResponse,
    VersionInfo,
)
from research_engine_client.models._base import RequestModel
from research_engine_client.models.document import Document, FetchMethod, Provenance, Quality
from research_engine_client.schemas import render_schemas

ROOT = Path(__file__).resolve().parents[2]


def test_registry_contains_core_models() -> None:
    for name in (
        "Document",
        "SearchRequest",
        "SearchResponse",
        "Job",
        "JobDetail",
        "Event",
        "ErrorDetail",
        "Envelope_SearchResponse_",
        "Envelope_Document_",
    ):
        assert name in ALL_MODELS, name


def test_committed_schemas_are_current() -> None:
    rendered = render_schemas()
    for name, text in rendered.items():
        path = ROOT / "schemas" / f"{name}.json"
        assert path.exists(), f"missing {path}; run: uv run research-engine schemas export"
        assert path.read_text() == text, f"stale {path}; run: uv run research-engine schemas export"


def _sample_document() -> Document:
    return Document(
        url="https://a.example",
        final_url="https://a.example",
        status=200,
        title="t",
        language="en",
        markdown="x",
        word_count=1,
        provenance=Provenance(
            url="https://a.example",
            fetched_at=datetime.now(UTC),
            content_hash="sha256:" + "0" * 64,
            method=FetchMethod.STATIC,
        ),
        quality=Quality(word_count=1, text_html_ratio=0.1, has_title=True),
    )


@pytest.mark.parametrize(
    ("name", "instance"),
    [
        ("Document", _sample_document()),
        (
            "Envelope_SearchResponse_",
            Envelope[SearchResponse](
                data=SearchResponse(query="q"), meta=Meta(request_id="r", took_ms=1)
            ),
        ),
    ],
)
def test_instances_validate_against_schema(name: str, instance: BaseModel) -> None:
    schema = json.loads(render_schemas()[name])
    jsonschema.Draft202012Validator(schema).validate(instance.model_dump(mode="json"))


def _object_schemas(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found = [("<root>", schema)] if "properties" in schema else []
    found += [(n, d) for n, d in schema.get("$defs", {}).items() if "properties" in d]
    return found


def _is_response(model: type[BaseModel]) -> bool:
    return not issubclass(model, RequestModel)


def test_orphaned_schema_files_are_detected() -> None:
    on_disk = {p.stem for p in (ROOT / "schemas").glob("*.json")}
    assert on_disk == set(render_schemas())


@pytest.mark.parametrize("name", sorted(n for n, m in ALL_MODELS.items() if _is_response(m)))
def test_response_schema_requires_every_property(name: str) -> None:
    schema = json.loads(render_schemas()[name])
    for where, obj in _object_schemas(schema):
        missing = set(obj["properties"]) - set(obj.get("required", []))
        assert not missing, f"{name} {where}: optional-looking properties {sorted(missing)}"


def test_omitted_nullable_field_fails_validation() -> None:
    dumped = ErrorDetail(code=ErrorCode.NOT_FOUND, message="x", retryable=False).model_dump(
        mode="json"
    )
    validator = jsonschema.Draft202012Validator(json.loads(render_schemas()["ErrorDetail"]))
    validator.validate(dumped)
    del dumped["source"]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(dumped)


def test_request_schema_only_requires_mandatory_fields() -> None:
    schema = json.loads(render_schemas()["SearchRequest"])
    assert schema["required"] == ["query"]
    assert json.loads(render_schemas()["FetchRequest"])["required"] == ["url"]


def test_datetimes_normalised_to_utc() -> None:
    ev = Event.model_validate(
        {"ts": "2026-01-01T12:00:00+02:00", "level": "info", "kind": "job.done", "message": "m"}
    )
    assert ev.ts.utcoffset() == timedelta(0)
    assert ev.ts == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert ev.model_dump(mode="json")["ts"].endswith("Z")
    with pytest.raises(ValidationError):
        Event.model_validate(
            {"ts": "2026-01-01T12:00:00", "level": "info", "kind": "job.done", "message": "m"}
        )


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "1.0.0-rc1", ""])
def test_schema_version_must_be_semver(bad: str) -> None:
    with pytest.raises(ValidationError):
        Meta(request_id="r", took_ms=1, schema_version=bad)
    with pytest.raises(ValidationError):
        VersionInfo(version="1", git_sha="a", schema_version=bad)
