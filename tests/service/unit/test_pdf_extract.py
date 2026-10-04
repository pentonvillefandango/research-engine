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
    assert ex.warnings == []
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


def _pdf_with_info_object(info: bytes) -> bytes:
    """Minimal 1-page PDF whose trailer /Info points at an arbitrary raw object body."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
        info,
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 4 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def test_deeply_nested_object_is_extraction_failed() -> None:
    """pypdf raises a raw RecursionError (not a PyPdfError) for 20k nested arrays."""
    with pytest.raises(ServiceError) as ei:
        PypdfExtractor().extract(_pdf_with_info_object(b"[" * 20000), "https://x.example/a.pdf")
    assert ei.value.detail.code is ErrorCode.EXTRACTION_FAILED
    assert ei.value.detail.retryable is False


def test_page_cap_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_extract, "MAX_PAGES", 1)
    ex = PypdfExtractor().extract(PDF.read_bytes(), "https://vendor.example/sheet.pdf")
    assert "pdf pages capped at 1" in ex.warnings
