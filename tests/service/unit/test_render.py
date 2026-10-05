from html.parser import HTMLParser

import pytest
from research_engine.gui.render import render_untrusted_markdown, sanitize_html


class _Tags(HTMLParser):
    """Collects the real elements and attributes of an HTML fragment (escaped text is ignored)."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def _elements(html: str) -> list[tuple[str, dict[str, str | None]]]:
    p = _Tags()
    p.feed(html)
    p.close()
    return p.elements


def _assert_inert(html: str) -> None:
    for tag, attrs in _elements(html):
        assert tag not in {"script", "img", "iframe", "svg", "style", "object", "embed"}, tag
        assert not any(a.startswith("on") for a in attrs), (tag, attrs)
        assert "style" not in attrs, (tag, attrs)
        href = (attrs.get("href") or "").strip().lower()
        assert not href.startswith(("javascript:", "data:", "vbscript:")), href


def test_strips_dangerous() -> None:
    out = render_untrusted_markdown(
        "# Hi\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n"
        "<img src=x onerror=alert(1)>\n\n"
        '<iframe src="https://e.example"></iframe>\n\n<p style="color:red">p</p>'
    )
    low = out.lower()
    # Raw HTML in the markdown is escaped to inert text, never emitted as markup.
    for bad in ("<script", "<img", "<iframe", "<p style"):
        assert bad not in low
    _assert_inert(out)
    assert not any(tag == "a" for tag, _ in _elements(out))  # the javascript: link is not a link
    assert "<h1>hi</h1>" in low


def test_keeps_safe_markup() -> None:
    out = render_untrusted_markdown(
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n- item\n\n`code`\n\n[ok](https://ok.example)"
    )
    assert "<table>" in out and "<li>item</li>" in out and "<code>code</code>" in out
    assert 'href="https://ok.example"' in out and "noopener" in out


def test_keeps_headings_lists_code_blocks() -> None:
    out = render_untrusted_markdown("## Two\n\n1. one\n\n```\nx < y\n```\n")
    assert "<h2>Two</h2>" in out and "<ol>" in out and "<pre><code>x &lt; y" in out


def test_link_rel_is_exact() -> None:
    out = render_untrusted_markdown("[ok](https://ok.example)")
    (_, attrs), *_ = [e for e in _elements(out) if e[0] == "a"]
    assert attrs["rel"] == "noopener noreferrer nofollow"


def test_uppercase_scheme_link_keeps_rel() -> None:
    out = render_untrusted_markdown("[ok](HTTPS://ok.example)")
    [(_, attrs)] = [e for e in _elements(out) if e[0] == "a"]
    assert attrs["href"] == "HTTPS://ok.example"
    assert attrs["rel"] == "noopener noreferrer nofollow"


def test_data_url_link_stripped() -> None:
    out = render_untrusted_markdown("[d](data:text/html;base64,PHNjcmlwdD4=)")
    assert "data:text/html" not in [a.get("href") for _, a in _elements(out)]
    _assert_inert(out)


# The nh3 layer on its own: what if raw HTML ever reaches it (defence in depth)?
@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("<script>alert(1)</script>ok", ("<script", "alert(1)")),
        ('<img src=x onerror="alert(1)">', ("<img", "onerror")),
        ('<a href="javascript:alert(1)">j</a>', ("javascript:",)),
        ('<a href="jav&#x09;ascript:alert(1)">j</a>', ("javascript", "ascript:")),
        ('<a href="data:text/html,<b>x</b>">d</a>', ("data:",)),
        ('<a href="HTTPS://ok.example" onclick="x()">h</a>', ("onclick",)),
        ('<iframe src="https://e.example"></iframe>', ("<iframe",)),
        ('<p style="color:red">p</p>', ("style=",)),
        ("<svg onload=alert(1)><circle/></svg>", ("<svg", "onload", "<circle")),
        ("<svg><script>alert(1)</script></svg>", ("<svg", "<script", "alert(1)")),
        ("<style>body{display:none}</style>t", ("<style", "display:none")),
        ('<form action="https://evil"><input name=x></form>', ("<form", "<input")),
    ],
)
def test_sanitizer_layer_strips(raw: str, forbidden: tuple[str, ...]) -> None:
    out = sanitize_html(raw)
    for bad in forbidden:
        assert bad not in out.lower(), (raw, out)
    _assert_inert(out)


def test_sanitizer_layer_keeps_https_link_with_rel() -> None:
    out = sanitize_html('<a href="HTTPS://ok" title="t">x</a>')
    [(_, attrs)] = _elements(out)
    assert attrs == {"href": "HTTPS://ok", "title": "t", "rel": "noopener noreferrer nofollow"}


def test_returns_markup_that_jinja_does_not_re_escape() -> None:
    from jinja2 import Environment
    from markupsafe import Markup

    out = render_untrusted_markdown("**b** <i>raw</i>")
    assert isinstance(out, Markup)
    page = Environment(autoescape=True).from_string("<div>{{ body }}</div>").render(body=out)
    assert page == f"<div>{out}</div>" and "<strong>b</strong>" in page
    assert "&lt;i&gt;raw&lt;/i&gt;" in page  # the escaped raw HTML stays escaped once, not twice
