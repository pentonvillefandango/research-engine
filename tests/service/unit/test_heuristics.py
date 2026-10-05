from pathlib import Path

from research_engine.pipeline.heuristics import looks_js_rendered

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "pages"


def test_spa_detected() -> None:
    assert looks_js_rendered((PAGES / "spa.html").read_text(), word_count=0, threshold=150)


def test_article_not_js() -> None:
    assert not looks_js_rendered(
        (PAGES / "article.html").read_text(), word_count=400, threshold=150
    )
