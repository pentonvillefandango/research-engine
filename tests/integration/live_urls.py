"""Live URLs for the fetch acceptance tests (step 3, Task 3.7).

SPA choice, measured 2026-10-04 against the dev stack (static = StaticFetcher +
DefaultHtmlExtractor word count; browser = Crawl4AI markdown word count). The criterion is
static < 150 and browser >= 150.
The brief's candidates, in order, all failed:
  https://www.crawl4ai.com/      static 634, browser 802   (server-rendered: static too rich)
  https://excalidraw.com/        static 10,  browser 83    (canvas app: too little text)
  https://app.diagrams.net/      static 64,  browser 65    (canvas app: too little text)
Additional public, robots-permitted JS-rendered pages measured:
  https://quotes.toscrape.com/js/  static 6,  browser 248   <- chosen (stable; built for scraping)
  https://crates.io/crates/serde   static 12, browser 0     (renders nothing in time)
  https://hn.algolia.com/          static 8,  browser 1219  (qualifies, but content changes daily)

PDF: the brief's https://www.rfc-editor.org/rfc/pdfrfc/rfc9110.txt.pdf now returns 404; the
canonical RFC 9110 PDF below is the same document (robots.txt allows /rfc/).
"""

STATIC_ARTICLE = "https://docs.python.org/3/library/asyncio-task.html"
SPA = "https://quotes.toscrape.com/js/"
PDF = "https://www.rfc-editor.org/rfc/rfc9110.pdf"
