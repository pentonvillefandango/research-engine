from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from research_engine_client.models.document import (
    Document,
    DocumentFormat,
    FetchMethod,
    FetchMode,
    FetchRequest,
    Link,
    Provenance,
    Quality,
    StructuredData,
    Table,
)

HASH = "sha256:" + "a" * 64


def _doc(**kw: object) -> Document:
    base: dict[str, object] = dict(
        url="https://a.example/p",
        final_url="https://a.example/p",
        status=200,
        title="T",
        language="en",
        markdown="# T",
        word_count=1,
        provenance=Provenance(
            url="https://a.example/p",
            fetched_at=datetime.now(UTC),
            content_hash=HASH,
            method=FetchMethod.STATIC,
        ),
        quality=Quality(word_count=1, text_html_ratio=0.5, has_title=True),
    )
    base.update(kw)
    return Document.model_validate(base)


def test_fetch_request_defaults() -> None:
    r = FetchRequest(url="https://a.example")
    assert r.mode is FetchMode.AUTO and r.formats == (DocumentFormat.MARKDOWN,)
    assert r.timeout_s == 60 and r.use_cache is True


@pytest.mark.parametrize("url", ["ftp://a.example", "file:///etc/passwd", "javascript:alert(1)"])
def test_fetch_request_rejects_non_http(url: str) -> None:
    with pytest.raises(ValidationError):
        FetchRequest(url=url)


@pytest.mark.parametrize("t", [0, 301])
def test_timeout_bounds(t: int) -> None:
    with pytest.raises(ValidationError):
        FetchRequest(url="https://a.example", timeout_s=t)


def test_document_defaults_and_nulls() -> None:
    d = _doc().model_dump(mode="json")
    for key in ("author", "published_at", "html"):
        assert key in d and d[key] is None
    assert d["links"] == [] and d["tables"] == [] and d["warnings"] == []
    assert d["structured_data"] == {"json_ld": [], "microdata": [], "opengraph": {}}
    assert d["provenance"]["job_id"] is None


def test_content_hash_format_enforced() -> None:
    with pytest.raises(ValidationError):
        Provenance(
            url="https://a.example",
            fetched_at=datetime.now(UTC),
            content_hash="md5:abc",
            method=FetchMethod.STATIC,
        )


def test_table_shape() -> None:
    t = Table(headers=["a", "b"], rows=[["1", "2"]])
    assert t.caption is None and t.source_selector is None


def test_link_model() -> None:
    assert Link(url="https://b.example", text="B", external=True).external


def test_structured_data_defaults() -> None:
    assert StructuredData().json_ld == []
