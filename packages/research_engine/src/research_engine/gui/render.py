"""Render untrusted fetched content safely (§1 'treat all fetched content as untrusted').

Two layers: markdown-it with ``html: False`` escapes any raw HTML in the markdown, then the nh3
allow-list removes anything that is not plain document markup. Only this function's output may
be marked safe, and it already is: it returns ``Markup``, so templates never need ``|safe``.
"""

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_TAGS = {"a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "dl", "dt", "em", "h1", "h2",
         "h3", "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "s", "strong", "sub", "sup",
         "table", "tbody", "td", "th", "thead", "tr", "ul"}  # fmt: skip
_ATTRIBUTES = {"a": {"href", "title"}, "th": {"align"}, "td": {"align"}}
_URL_SCHEMES = {"http", "https", "mailto"}
LINK_REL = "noopener noreferrer nofollow"


def sanitize_html(html: str) -> str:
    """The nh3 allow-list layer on its own."""
    return nh3.clean(
        html, tags=_TAGS, attributes=_ATTRIBUTES, url_schemes=_URL_SCHEMES, link_rel=LINK_REL
    )


def render_untrusted_markdown(text: str) -> Markup:
    return Markup(sanitize_html(_MD.render(text)))  # noqa: S704  # nh3 allow-list output
