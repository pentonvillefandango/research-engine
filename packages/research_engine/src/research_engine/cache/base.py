"""Cache seam (V1-10). Keys are sha256 of kind + canonical request JSON."""

import hashlib
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl_s: int) -> None: ...
    def stats(self) -> CacheStats: ...


def cache_key(kind: str, payload: BaseModel | str) -> str:
    body = (
        payload.model_dump_json(exclude={"use_cache"})
        if isinstance(payload, BaseModel)
        else payload
    )
    return hashlib.sha256(f"{kind}\x00{body}".encode()).hexdigest()
