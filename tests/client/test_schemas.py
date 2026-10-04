import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from pydantic import BaseModel
from research_engine_client.models import ALL_MODELS, Envelope, Meta, SearchResponse
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
