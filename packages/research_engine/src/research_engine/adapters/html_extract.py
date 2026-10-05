"""HTML to markdown/metadata (Trafilatura), tables (lxml), links, embedded data (extruct).

V1-04, V1-05. Synchronous by design: callers run it via asyncio.to_thread
(Global Constraint 6).

Guards against hostile or huge input. Each one appends a short string to
``Extracted.warnings``:
- HTML longer than ``MAX_HTML_CHARS`` is truncated before parsing
  ("html truncated to N chars"); ``html_len`` still reports the original length.
- Trafilatura is quadratic in the number of tables, so above
  ``MAX_TABLES_FOR_TRAFILATURA`` it runs with ``include_tables=False``
  ("trafilatura tables disabled"); our own lxml extractor still provides the tables.
- Above ``MAX_ELEMENTS_FOR_TRAFILATURA`` elements Trafilatura is skipped and the
  markdown is the whitespace-normalised body text ("trafilatura skipped") and
  ``extract_metadata`` is skipped too (it is equally slow); title, author and date
  then come from ``<title>``, ``<meta name="author">`` and
  ``article:published_time``/``<time datetime>``.
- At most ``MAX_LINKS`` links, ``MAX_TABLES`` tables ("tables capped at N") and
  ``MAX_TABLE_ROWS`` rows per table ("table rows capped at N") are returned.

Table notes: only a table's own rows and thead are read (those of nested tables
belong to the nested table). ``rowspan``/``colspan`` are ignored; cells are taken
as they appear. ``source_selector`` numbers every ``<table>`` in document order
(nested ones included), which is an approximation of CSS ``nth-of-type``
accepted for V1.
"""

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import extruct
import trafilatura
from lxml import html as lxml_html
from lxml.etree import LxmlError
from research_engine_client.models import Link, StructuredData, Table

from research_engine.pipeline.urls import domain_of, is_http_url

from .extract import Extracted

MAX_HTML_CHARS = 5_000_000
MAX_LINKS = 500
MAX_TABLES = 100
MAX_TABLE_ROWS = 1000
MAX_TABLES_FOR_TRAFILATURA = 200
# Trafilatura extract plus extract_metadata cost about 65 us/element: about 3 s at the cap.
MAX_ELEMENTS_FOR_TRAFILATURA = 40_000

_LANG = re.compile(r"^([A-Za-z]{2,3})(?:[-_]|$)")


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, OverflowError):
        return None


def _norm_lang(value: str | None) -> str | None:
    m = _LANG.match((value or "").strip())
    return m.group(1).lower() if m else None


class DefaultHtmlExtractor:
    def extract(self, html: str, base_url: str) -> Extracted:
        original_len = len(html)
        warnings: list[str] = []
        if original_len > MAX_HTML_CHARS:
            html = html[:MAX_HTML_CHARS]
            warnings.append(f"html truncated to {MAX_HTML_CHARS} chars")
        tree = self._tree(html)
        markdown, skipped = self._markdown(html, tree, base_url, warnings)
        if skipped:
            meta_title, meta_author, meta_date, meta_lang = self._fallback_meta(tree)
        else:
            meta = trafilatura.extract_metadata(html, default_url=base_url)
            meta_title = meta.title if meta else None
            meta_author = meta.author if meta else None
            meta_date = meta.date if meta else None
            meta_lang = meta.language if meta else None
        title = _norm(meta_title) or (_norm(tree.findtext(".//title")) if tree is not None else "")
        language = _norm_lang(meta_lang) or _norm_lang(
            tree.get("lang") if tree is not None else None
        )
        tables, table_warnings = self._tables(tree) if tree is not None else ([], [])
        warnings.extend(table_warnings)
        return Extracted(
            title=title or None,
            author=_norm(meta_author) or None,
            published_at=_parse_date(meta_date),
            language=language,
            markdown=markdown,
            word_count=len(markdown.split()),
            links=self._links(tree, base_url) if tree is not None else [],
            tables=tables,
            structured_data=self._structured(html, base_url),
            html_len=original_len,
            text_len=len(" ".join(markdown.split())),
            warnings=warnings,
        )

    @staticmethod
    def _markdown(html: str, tree: Any, base_url: str, warnings: list[str]) -> tuple[str, bool]:
        """Return (markdown, trafilatura_skipped)."""
        include_tables = True
        if tree is not None:
            if len(tree.xpath("//*")) > MAX_ELEMENTS_FOR_TRAFILATURA:
                warnings.append("trafilatura skipped")
                text = " ".join(
                    tree.xpath(
                        "//body//text()[not(ancestor::script or ancestor::style"
                        " or ancestor::noscript)]"
                    )
                )
                return _norm(text), True
            if len(tree.xpath("//table")) > MAX_TABLES_FOR_TRAFILATURA:
                include_tables = False
                warnings.append("trafilatura tables disabled")
        markdown = trafilatura.extract(
            html,
            url=base_url,
            output_format="markdown",
            include_tables=include_tables,
            include_links=False,
            include_comments=False,
            favor_recall=True,
        )
        return markdown or "", False

    @staticmethod
    def _fallback_meta(tree: Any) -> tuple[str | None, str | None, str | None, str | None]:
        if tree is None:
            return None, None, None, None

        def first(xpath: str) -> str | None:
            found = tree.xpath(xpath)
            return str(found[0]) if found else None

        return (
            first("//head/title/text()"),
            first("//meta[@name='author']/@content"),
            first("//meta[@property='article:published_time']/@content")
            or first("//time/@datetime"),
            None,
        )

    @staticmethod
    def _tree(html: str) -> Any:
        try:
            return lxml_html.document_fromstring(html)
        except (ValueError, LxmlError):
            return None

    @staticmethod
    def _links(tree: Any, base_url: str) -> list[Link]:
        base_domain = domain_of(base_url)
        seen: dict[str, Link] = {}
        for a in tree.iterfind(".//a[@href]"):
            try:
                url = urljoin(base_url, a.get("href", "").strip())
            except ValueError:
                continue
            if url in seen or not is_http_url(url):
                continue
            seen[url] = Link(
                url=url, text=_norm(a.text_content()), external=domain_of(url) != base_domain
            )
            if len(seen) >= MAX_LINKS:
                break
        return list(seen.values())

    @staticmethod
    def _table(table: Any, idx: int, warnings: list[str]) -> Table | None:
        depth = len(list(table.iterancestors("table"))) + 1
        trs = table.xpath(".//tr[count(ancestor::table)=$d]", d=depth)[: MAX_TABLE_ROWS + 2]
        cell_rows = [[c for c in tr if c.tag in ("td", "th")] for tr in trs]
        cell_rows = [r for r in cell_rows if r]
        if len(cell_rows) < 2 or max(len(r) for r in cell_rows) < 2:
            return None
        rows = [[_norm(c.text_content()) for c in r] for r in cell_rows]
        has_header = table.find("./thead") is not None or all(c.tag == "th" for c in cell_rows[0])
        headers, body = (rows[0], rows[1:]) if has_header else ([], rows)
        if len(body) > MAX_TABLE_ROWS:
            body = body[:MAX_TABLE_ROWS]
            msg = f"table rows capped at {MAX_TABLE_ROWS}"
            if msg not in warnings:
                warnings.append(msg)
        caption = table.find("./caption")
        return Table(
            caption=_norm(caption.text_content()) if caption is not None else None,
            headers=headers,
            rows=body,
            source_selector=f"table:nth-of-type({idx})",
        )

    @classmethod
    def _tables(cls, tree: Any) -> tuple[list[Table], list[str]]:
        out: list[Table] = []
        warnings: list[str] = []
        for idx, table in enumerate(tree.iterfind(".//table"), start=1):
            if len(out) >= MAX_TABLES:
                # Only warn if a further usable table exists.
                if cls._table(table, idx, []) is not None:
                    warnings.append(f"tables capped at {MAX_TABLES}")
                    break
                continue
            parsed = cls._table(table, idx, warnings)
            if parsed is not None:
                out.append(parsed)
        return out, warnings

    @staticmethod
    def _structured(html: str, base_url: str) -> StructuredData:
        try:
            data = extruct.extract(
                html,
                base_url=base_url,
                syntaxes=["json-ld", "microdata", "opengraph"],
                uniform=True,
                errors="ignore",
            )
        except Exception:  # extruct raises many types on hostile markup; degrade to empty
            return StructuredData()
        og: dict[str, Any] = {}
        for item in data.get("opengraph", []):
            for k, v in item.items():
                if k != "@context":
                    og.setdefault(k if ":" in k else f"og:{k}", v)
        return StructuredData(
            json_ld=data.get("json-ld", []), microdata=data.get("microdata", []), opengraph=og
        )
