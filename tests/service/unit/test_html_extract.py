import time
from datetime import timedelta
from pathlib import Path

import pytest
from research_engine.adapters import html_extract
from research_engine.adapters.html_extract import DefaultHtmlExtractor

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "pages"
X = DefaultHtmlExtractor()


def test_article() -> None:
    ex = X.extract((PAGES / "article.html").read_text(), "https://blog.example/post")
    assert ex.word_count >= 300 and ex.title and ex.author == "Test Author" and ex.language == "en"
    assert ex.published_at is not None and ex.published_at.tzinfo is not None
    assert "# " in ex.markdown or ex.markdown.lstrip().startswith("#")
    assert all(link.url.startswith("http") for link in ex.links)
    assert {link.external for link in ex.links} == {True, False}


def test_structured_and_tables() -> None:
    ex = X.extract((PAGES / "jsonld_tables.html").read_text(), "https://shop.example/w")
    assert any(item.get("@type") == "Product" for item in ex.structured_data.json_ld)
    assert ex.structured_data.opengraph.get("og:title") == "Widget page"
    assert len(ex.structured_data.microdata) == 1
    assert len(ex.tables) == 2
    t1, t2 = ex.tables
    assert (
        t1.caption == "Plans"
        and t1.headers == ["Plan", "Price"]
        and t1.rows == [["Free", "0"], ["Pro", "10"]]
    )
    assert t1.source_selector == "table:nth-of-type(1)"
    assert t2.headers == ["A", "B"] and t2.rows == [["1", "2"]]
    assert t2.source_selector == "table:nth-of-type(2)"


def test_malformed_html_does_not_raise() -> None:
    X.extract("<html><body><table><tr><td>x", "https://a.example/")


def test_empty_html_does_not_raise() -> None:
    ex = X.extract("", "https://a.example/")
    assert ex.markdown == "" and ex.links == [] and ex.tables == []


def test_link_cap_and_dedupe() -> None:
    html = (
        "<html><body>"
        + "".join(f'<a href="/p{i % 600}"> L  {i} </a>' for i in range(1200))
        + "</body></html>"
    )
    ex = X.extract(html, "https://a.example/")
    assert len(ex.links) == 500 and ex.links[0].text == "L 0"


def test_links_only_http_resolved_and_external() -> None:
    html = (
        "<html><body>"
        '<a href="javascript:alert(1)">js</a><a href="data:text/html,x">d</a>'
        '<a href="mailto:a@b.example">m</a><a href="tel:123">t</a><a href="#frag">f</a>'
        '<a href="rel/page">  Rel \n page </a><a href="https://www.other.example/x">Out</a>'
        '<a href="https://blog.example/same">Same</a>'
        "</body></html>"
    )
    ex = X.extract(html, "https://blog.example/dir/")
    by_url = {link.url: link for link in ex.links}
    assert by_url["https://blog.example/dir/rel/page"].text == "Rel page"
    assert not by_url["https://blog.example/dir/rel/page"].external
    assert by_url["https://www.other.example/x"].external
    assert not by_url["https://blog.example/same"].external
    assert not any(u.startswith(("javascript", "data", "mailto", "tel")) for u in by_url)


def test_tables_adversarial() -> None:
    nested = (
        "<table><tr><td><table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
        "</td><td>x</td></tr><tr><td>y</td><td>z</td></tr></table>"
    )
    irregular = (
        '<table><tr><th colspan="2">H</th></tr><tr><td rowspan="2">1</td><td>2</td></tr>'
        "<tr><td>3</td></tr></table>"
    )
    ex = X.extract(f"<html><body>{nested}{irregular}</body></html>", "https://a.example/")
    assert len(ex.tables) == 3
    assert ex.tables[0].rows == [["abcd", "x"], ["y", "z"]]  # outer cell text includes nested text
    assert ex.tables[1].rows == [["a", "b"], ["c", "d"]]
    assert ex.tables[-1].headers == ["H"]


def test_table_caps() -> None:
    many = "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>" * 300
    ex = X.extract(f"<html><body>{many}</body></html>", "https://a.example/")
    assert len(ex.tables) == html_extract.MAX_TABLES
    rows = "<tr><td>a</td><td>b</td></tr>" * 3000
    ex = X.extract(f"<html><body><table>{rows}</table></body></html>", "https://a.example/")
    assert len(ex.tables) == 1 and len(ex.tables[0].rows) <= html_extract.MAX_TABLE_ROWS


def test_oversized_html_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(html_extract, "MAX_HTML_CHARS", 200)
    html = "<html><body><p>" + "word " * 5000 + "</p></body></html>"
    ex = X.extract(html, "https://a.example/")
    assert ex.html_len == len(html)
    assert "html truncated to 200 chars" in ex.warnings


TINY_TABLE = "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"


def test_many_tables_do_not_blow_up_trafilatura() -> None:
    html = f"<html><body><p>Intro text here.</p>{TINY_TABLE * 5000}</body></html>"
    start = time.perf_counter()
    ex = X.extract(html, "https://a.example/")
    assert time.perf_counter() - start < 3
    assert len(ex.tables) == html_extract.MAX_TABLES
    assert "trafilatura tables disabled" in ex.warnings
    assert f"tables capped at {html_extract.MAX_TABLES}" in ex.warnings


def test_huge_element_count_skips_trafilatura() -> None:
    html = "<html><body>" + "<div><p>tiny words</p></div>" * 50_000 + "</body></html>"
    start = time.perf_counter()
    ex = X.extract(html, "https://a.example/")
    assert time.perf_counter() - start < 5
    assert "trafilatura skipped" in ex.warnings
    assert ex.markdown.startswith("tiny words") and ex.word_count == 100_000


def test_fallback_metadata_when_trafilatura_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(html_extract, "MAX_ELEMENTS_FOR_TRAFILATURA", 3)
    ex = X.extract(
        '<html lang="en"><head><title>T</title><meta name="author" content="Test Author">'
        '<meta property="article:published_time" content="2026-03-14T09:00:00Z"></head>'
        "<body><p>a</p><p>b</p></body></html>",
        "https://a.example/",
    )
    assert "trafilatura skipped" in ex.warnings
    assert ex.title == "T" and ex.author == "Test Author" and ex.language == "en"
    assert ex.published_at is not None and ex.published_at.year == 2026


def test_fallback_text_ignores_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(html_extract, "MAX_ELEMENTS_FOR_TRAFILATURA", 3)
    ex = X.extract(
        "<html><body><script>var x=1</script><style>p{}</style><p>a</p><p> b\n c</p></body></html>",
        "https://a.example/",
    )
    assert ex.markdown == "a b c"


def test_row_cap_warning() -> None:
    rows = "<tr><td>a</td><td>b</td></tr>" * 1500
    ex = X.extract(f"<html><body><table>{rows}</table></body></html>", "https://a.example/")
    assert f"table rows capped at {html_extract.MAX_TABLE_ROWS}" in ex.warnings


def test_no_warnings_on_normal_page() -> None:
    ex = X.extract((PAGES / "article.html").read_text(), "https://blog.example/post")
    assert ex.warnings == []


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en", "en"),
        ("EN-us", "en"),
        ("en_US", "en"),
        ("EN_us", "en"),
        ("fil", "fil"),
        ("x" * 5000, None),
        ("\u202een", None),
        ("   ", None),
        ("", None),
        ("e", None),
        ("english", None),
        ("1234", None),
    ],
)
def test_language_normalised(lang: str, expected: str | None) -> None:
    ex = X.extract(f'<html lang="{lang}"><body><p>hello</p></body></html>', "https://a.example/")
    assert ex.language == expected


@pytest.mark.parametrize(
    "value",
    ["9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59", "garbage", "", None],
)
def test_parse_date_extremes_return_none(value: str | None) -> None:
    assert html_extract._parse_date(value) is None  # pyright: ignore[reportPrivateUsage]


def test_parse_date_normalises_to_utc() -> None:
    dt = html_extract._parse_date("2026-03-14T10:00:00+02:00")  # pyright: ignore[reportPrivateUsage]
    assert dt is not None and dt.utcoffset() == timedelta(0)
    assert dt.hour == 8
    naive = html_extract._parse_date("2026-03-14")  # pyright: ignore[reportPrivateUsage]
    assert naive is not None and naive.tzinfo is not None


def test_nested_thead_does_not_make_outer_header() -> None:
    html = (
        "<html><body><table><tr><td><table><thead><tr><th>i</th><th>j</th></tr></thead>"
        "<tr><td>1</td><td>2</td></tr></table></td><td>x</td></tr>"
        "<tr><td>y</td><td>z</td></tr></table></body></html>"
    )
    ex = X.extract(html, "https://a.example/")
    outer, inner = ex.tables
    assert outer.headers == [] and len(outer.rows) == 2
    assert inner.headers == ["i", "j"]
