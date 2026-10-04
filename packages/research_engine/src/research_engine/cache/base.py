"""Cache seam (V1-10). Keys are sha256 of kind + canonical request JSON."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

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


def _drop_use_cache(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_use_cache(v) for k, v in value.items() if k != "use_cache"}
    if isinstance(value, list):
        return [_drop_use_cache(v) for v in value]
    return value


def cache_key(kind: str, payload: BaseModel | str) -> str:
    """sha256 of kind + canonical JSON; ``use_cache`` is dropped at every depth."""
    if isinstance(payload, BaseModel):
        body = json.dumps(
            _drop_use_cache(payload.model_dump(mode="json")), sort_keys=True, separators=(",", ":")
        )
    else:
        body = payload
    return hashlib.sha256(f"{kind}\x00{body}".encode()).hexdigest()
