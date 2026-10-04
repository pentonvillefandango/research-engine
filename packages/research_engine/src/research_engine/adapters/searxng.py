"""SearXNG JSON API adapter (V1-01, V1-03). AGPL service, called over HTTP only (D14)."""

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import ValidationError
from research_engine_client.models import (
    ErrorCode,
    Infobox,
    InfoboxLink,
    TimeRange,
    UnresponsiveEngine,
)

from research_engine.errors import ServiceError

from .search import RawHit, RawSearchPage

PAGE_SIZE = 10


def _invalid(detail: str) -> ServiceError:
    return ServiceError.of(
        ErrorCode.UPSTREAM_ERROR,
        f"invalid SearXNG response: {detail}",
        retryable=False,
        source="searxng",
    )


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class SearxngProvider:
    def __init__(self, base_url: str, client: httpx.AsyncClient, timeout_s: float) -> None:
        self._base = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout_s

    async def search(
        self,
        query: str,
        *,
        categories: list[str],
        engines: list[str],
        language: str,
        time_range: TimeRange | None,
        pageno: int,
    ) -> RawSearchPage:
        params: dict[str, str] = {
            "q": query,
            "format": "json",
            "language": language,
            "pageno": str(pageno),
            "safesearch": "0",
        }
        if engines:
            params["engines"] = ",".join(engines)
        else:
            params["categories"] = ",".join(categories)
        if time_range is not None:
            params["time_range"] = time_range.value
        try:
            resp = await self._client.get(
                f"{self._base}/search", params=params, timeout=self._timeout
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ServiceError.of(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"SearXNG timed out: {exc}",
                retryable=True,
                source="searxng",
                http_status=504,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            msg = f"SearXNG returned {status}" + (
                "; enable json in search.formats" if status == 403 else ""
            )
            raise ServiceError.of(
                ErrorCode.UPSTREAM_ERROR, msg, retryable=status >= 500, source="searxng"
            ) from exc
        except httpx.HTTPError as exc:
            raise ServiceError.of(
                ErrorCode.UPSTREAM_ERROR,
                f"SearXNG unreachable: {exc}",
                retryable=True,
                source="searxng",
            ) from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise _invalid(f"body is not JSON ({exc})") from exc
        if not isinstance(body, dict) or not isinstance(body.get("results", []), list):
            raise _invalid("body is not a JSON object with a results list")
        return self._parse(body, pageno)

    @staticmethod
    def _parse_hit(r: Any, offset: int) -> RawHit | None:
        """Parse one result; None if malformed (the hit is skipped, not the page)."""
        if not isinstance(r, dict) or not isinstance(r.get("url"), str) or not r["url"]:
            return None
        try:
            raw_engines = r.get("engines") or ([r["engine"]] if r.get("engine") else [])
            raw_positions = r.get("positions") or [1]
            if not isinstance(raw_engines, list) or not isinstance(raw_positions, list):
                return None
            return RawHit(
                url=r["url"],
                title=str(r.get("title") or ""),
                content=str(r.get("content") or ""),
                engines=[str(e) for e in raw_engines],
                positions=[int(p) + offset for p in raw_positions],
                published=_parse_dt(r.get("publishedDate")),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_infobox(i: Any) -> Infobox | None:
        if not isinstance(i, dict):
            return None
        urls = i.get("urls")
        try:
            return Infobox(
                title=i.get("infobox") or "",
                content=i.get("content"),
                engine=i.get("engine"),
                urls=[
                    InfoboxLink(title=u.get("title", ""), url=u["url"])
                    for u in (urls if isinstance(urls, list) else [])
                    if isinstance(u, dict) and u.get("url")
                ],
            )
        except ValidationError:
            return None

    @classmethod
    def _parse(cls, body: dict[str, Any], pageno: int) -> RawSearchPage:
        offset = (pageno - 1) * PAGE_SIZE

        def items(key: str) -> list[Any]:
            v = body.get(key)
            return v if isinstance(v, list) else []

        hits = [h for r in items("results") if (h := cls._parse_hit(r, offset)) is not None]
        infoboxes = [b for i in items("infoboxes") if (b := cls._parse_infobox(i)) is not None]
        unresponsive = [
            UnresponsiveEngine(engine=str(e[0]), error=str(e[1]))
            for e in items("unresponsive_engines")
            if isinstance(e, list) and len(e) >= 2
        ]
        return RawSearchPage(
            hits=hits,
            suggestions=[s for s in items("suggestions") if isinstance(s, str)],
            infoboxes=infoboxes,
            unresponsive=unresponsive,
        )

    async def health(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base}/healthz", timeout=5)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200
