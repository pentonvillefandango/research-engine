"""Basic PDF text extraction with pypdf (V1-06). Docling replaces this in V2.

Synchronous by design: callers run it via asyncio.to_thread. At most
``MAX_PAGES`` pages are extracted; later pages are dropped and
``Extracted.warnings`` records it.
"""

import io

import structlog
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from research_engine_client.models import ErrorCode, StructuredData

from research_engine.errors import ServiceError

from .extract import Extracted

MAX_PAGES = 500

log = structlog.get_logger(__name__)

# pypdf leaks these on corrupted-but-plausible files (found by byte-fuzzing sample.pdf).
# AssertionError is caught because pypdf uses bare asserts; note that asserts vanish under
# `python -O`, so other failure types may surface there. RecursionError comes from deeply
# nested content streams. MemoryError is deliberately not caught.
_PYPDF_FAILURES = (
    PyPdfError,
    ValueError,
    KeyError,
    OSError,
    AttributeError,
    TypeError,
    AssertionError,
    RecursionError,
)


class PypdfExtractor:
    def extract(self, body: bytes, url: str) -> Extracted:
        try:
            reader = PdfReader(io.BytesIO(body))
            encrypted = reader.is_encrypted
            pages = (
                []
                if encrypted
                else [(p.extract_text() or "").strip() for p in reader.pages[:MAX_PAGES]]
            )
            page_count = 0 if encrypted else len(reader.pages)
            meta = None if encrypted else reader.metadata
        except _PYPDF_FAILURES as exc:
            log.debug("pdf_read_failed", url=url, exc_type=type(exc).__name__, error=str(exc))
            raise ServiceError.of(
                ErrorCode.EXTRACTION_FAILED,
                f"could not read PDF: {exc}",
                retryable=False,
                source=url,
            ) from exc
        if encrypted:
            raise ServiceError.of(
                ErrorCode.EXTRACTION_FAILED, "encrypted PDF", retryable=False, source=url
            )
        warnings = [f"pdf pages capped at {MAX_PAGES}"] if page_count > MAX_PAGES else []
        text = "\n\n---\n\n".join(p for p in pages if p)
        return Extracted(
            title=(meta.title if meta else None) or None,
            author=(meta.author if meta else None) or None,
            published_at=None,
            language=None,
            markdown=text,
            word_count=len(text.split()),
            links=[],
            tables=[],
            structured_data=StructuredData(),
            html_len=len(body),
            text_len=len(text),
            warnings=warnings,
        )
