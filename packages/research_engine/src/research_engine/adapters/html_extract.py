"""HTML to markdown/metadata (Trafilatura), tables (lxml), links, embedded data (extruct).

V1-04, V1-05. Synchronous by design: callers run it via asyncio.to_thread
(Global Constraint 6).

Guards against hostile or huge input:
- HTML longer than ``MAX_HTML_CHARS`` is truncated before parsing; nothing else
  is recorded (``Extracted.html_len`` reports the truncated length).
- At most ``MAX_LINKS`` links, ``MAX_TABLES`` tables and ``MAX_TABLE_ROWS`` rows
  per table are returned. ``Table`` has no truncation flag, so hitting a cap is
  silent to callers.

Table notes: only a table's own rows are read (rows of nested tables belong to
the nested table). ``rowspan``/``colspan`` are ignored; cells are taken as they
appear. ``source_selector`` numbers every ``<table>`` in document order
(nested ones included), which is an approximation of CSS ``nth-of-type``
accepted for V1.
"""

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


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class DefaultHtmlExtractor:
    def extract(self, html: str, base_url: str) -> Extracted:
        html = html[:MAX_HTML_CHARS]
        tree = self._tree(html)
        markdown = (
            trafilatura.extract(
                html,
                url=base_url,
                output_format="markdown",
                include_tables=True,
                include_links=False,
                include_comments=False,
                favor_recall=True,
            )
            or ""
        )
        meta = trafilatura.extract_metadata(html, default_url=base_url)
        title = _norm(meta.title if meta else None) or (
            _norm(tree.findtext(".//title")) if tree is not None else ""
        )
        lang = (meta.language if meta else None) or (tree.get("lang") if tree is not None else None)
        return Extracted(
            title=title or None,
            author=_norm(meta.author if meta else None) or None,
            published_at=_parse_date(meta.date if meta else None),
            language=(lang or "").split("-")[0].lower() or None,
            markdown=markdown,
            word_count=len(markdown.split()),
            links=self._links(tree, base_url) if tree is not None else [],
            tables=self._tables(tree) if tree is not None else [],
            structured_data=self._structured(html, base_url),
            html_len=len(html),
            text_len=len(" ".join(markdown.split())),
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
    def _tables(tree: Any) -> list[Table]:
        out: list[Table] = []
        for idx, table in enumerate(tree.iterfind(".//table"), start=1):
            depth = len(list(table.iterancestors("table"))) + 1
            trs = table.xpath(".//tr[count(ancestor::table)=$d]", d=depth)[: MAX_TABLE_ROWS + 1]
            cell_rows = [[c for c in tr if c.tag in ("td", "th")] for tr in trs]
            cell_rows = [r for r in cell_rows if r]
            if len(cell_rows) < 2 or max(len(r) for r in cell_rows) < 2:
                continue
            rows = [[_norm(c.text_content()) for c in r] for r in cell_rows]
            has_header = table.find(".//thead") is not None or all(
                c.tag == "th" for c in cell_rows[0]
            )
            headers, body = (rows[0], rows[1:]) if has_header else ([], rows)
            caption = table.find("./caption")
            out.append(
                Table(
                    caption=_norm(caption.text_content()) if caption is not None else None,
                    headers=headers,
                    rows=body[:MAX_TABLE_ROWS],
                    source_selector=f"table:nth-of-type({idx})",
                )
            )
            if len(out) >= MAX_TABLES:
                break
        return out

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
