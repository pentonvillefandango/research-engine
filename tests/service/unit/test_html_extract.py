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
    assert ex.html_len == 200
