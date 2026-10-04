"""SearXNG JSON API adapter (V1-01, V1-03). AGPL service, called over HTTP only (D14)."""

from datetime import UTC, datetime
from typing import Any

import httpx
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
        return self._parse(resp.json(), pageno)

    @staticmethod
    def _parse(body: dict[str, Any], pageno: int) -> RawSearchPage:
        offset = (pageno - 1) * PAGE_SIZE
        hits = []
        for r in body.get("results", []):
            url = r.get("url")
            if not url:
                continue
            engines = list(r.get("engines") or ([r["engine"]] if r.get("engine") else []))
            positions = [int(p) + offset for p in (r.get("positions") or [1])]
            hits.append(
                RawHit(
                    url=url,
                    title=r.get("title") or "",
                    content=r.get("content") or "",
                    engines=engines,
                    positions=positions,
                    published=_parse_dt(r.get("publishedDate")),
                )
            )
        infoboxes = [
            Infobox(
                title=i.get("infobox") or "",
                content=i.get("content"),
                engine=i.get("engine"),
                urls=[
                    InfoboxLink(title=u.get("title", ""), url=u["url"])
                    for u in i.get("urls") or []
                    if u.get("url")
                ],
            )
            for i in body.get("infoboxes", [])
        ]
        unresponsive = [
            UnresponsiveEngine(engine=str(e[0]), error=str(e[1]))
            for e in body.get("unresponsive_engines", [])
            if len(e) >= 2
        ]
        return RawSearchPage(
            hits=hits,
            suggestions=list(body.get("suggestions", [])),
            infoboxes=infoboxes,
            unresponsive=unresponsive,
        )

    async def health(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base}/healthz", timeout=5)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200
