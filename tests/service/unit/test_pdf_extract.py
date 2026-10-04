import io
import random
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from research_engine.adapters import pdf_extract
from research_engine.adapters.pdf_extract import PypdfExtractor
from research_engine.errors import ServiceError
from research_engine_client.models import ErrorCode

PDF = Path(__file__).resolve().parents[2] / "fixtures" / "pages" / "sample.pdf"


def test_sample_pdf() -> None:
    ex = PypdfExtractor().extract(PDF.read_bytes(), "https://vendor.example/sheet.pdf")
    assert "Widget Pro pricing" in ex.markdown and "Support hours" in ex.markdown
    assert "\n\n---\n\n" in ex.markdown
    assert ex.title == "Sample Vendor Sheet" and ex.author == "Test Author"
    assert ex.tables == [] and ex.links == [] and ex.language is None and ex.word_count > 20


@pytest.mark.parametrize(
    "body", [b"", b"%PDF-1.4 garbage", b"not a pdf at all", b"%PDF-1.4\n" + b"\x00" * 64]
)
def test_bad_pdf(body: bytes) -> None:
    with pytest.raises(ServiceError) as ei:
        PypdfExtractor().extract(body, "https://x.example/a.pdf")
    assert ei.value.detail.code is ErrorCode.EXTRACTION_FAILED
    assert ei.value.detail.retryable is False


def test_truncated_pdf() -> None:
    with pytest.raises(ServiceError) as ei:
        PypdfExtractor().extract(PDF.read_bytes()[:300], "https://x.example/a.pdf")
    assert ei.value.detail.code is ErrorCode.EXTRACTION_FAILED


def test_encrypted_pdf() -> None:
    w = PdfWriter(clone_from=PdfReader(PDF))
    w.encrypt("secret")
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(ServiceError) as ei:
        PypdfExtractor().extract(buf.getvalue(), "https://x.example/a.pdf")
    assert ei.value.detail.code is ErrorCode.EXTRACTION_FAILED
    assert ei.value.detail.retryable is False


def test_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_extract, "MAX_PAGES", 1)
    ex = PypdfExtractor().extract(PDF.read_bytes(), "https://vendor.example/sheet.pdf")
    assert "Widget Pro pricing" in ex.markdown and "Support hours" not in ex.markdown


def test_corrupted_pdf_only_raises_service_error() -> None:
    """Seeded byte-fuzzing: pypdf leaks AttributeError/TypeError/etc. on corrupt input."""
    rng = random.Random(1)  # noqa: S311 - deterministic fuzz seed, not security
    good = PDF.read_bytes()
    for _ in range(300):
        data = bytearray(good)
        for _ in range(rng.randint(1, 20)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        try:
            PypdfExtractor().extract(bytes(data), "https://x.example/a.pdf")
        except ServiceError as exc:
            assert exc.detail.code is ErrorCode.EXTRACTION_FAILED
