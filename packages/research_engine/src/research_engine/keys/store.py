"""Named API keys (V1-24, ADR-0028): SHA-256 hashes in SQLite, active ones mirrored in memory
so the auth middleware matches without a database round trip. One app process (one uvicorn
worker), so the in-memory map is authoritative after every create/revoke."""

import asyncio
import hashlib
import secrets
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.api.auth import key_matches
from research_engine.keys.models import ENV_CALLER, Caller, CallerKind
from research_engine.store.tables import ApiKeyRow

MAX_NAME = 64


class KeyNameError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _digest(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()


class KeyStore:
    def __init__(self, engine: AsyncEngine, env_key: bytes) -> None:
        self._engine = engine
        self._env_key = env_key
        self._active: dict[bytes, Caller] = {}
        self._lock = asyncio.Lock()

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def load(self) -> None:
        """Read the active keys from the database. Startup only; create/revoke keep the map."""
        async with self._lock:
            async with AsyncSession(self._engine) as s:
                stmt = select(ApiKeyRow).where(col(ApiKeyRow.revoked_at).is_(None))
                rows = (await s.exec(stmt)).all()
            self._active = {
                bytes.fromhex(r.key_hash): Caller(CallerKind.KEY, r.name, r.id) for r in rows
            }

    def match(self, supplied: bytes) -> Caller | None:
        if not supplied:
            return None
        if key_matches(supplied, self._env_key):
            return ENV_CALLER
        return self._active.get(_digest(supplied))

    async def create(self, name: str) -> tuple[ApiKeyRow, str]:
        name = name.strip()
        if not name or len(name) > MAX_NAME or not name.isprintable():
            raise KeyNameError(f"name must be 1-{MAX_NAME} printable characters")
        key = "re_" + secrets.token_urlsafe(32)
        digest = _digest(key.encode())
        row = ApiKeyRow(name=name, prefix=key[3:11], key_hash=digest.hex(), created_at=_now())
        async with self._lock:
            try:
                async with AsyncSession(self._engine, expire_on_commit=False) as s:
                    s.add(row)
                    await s.commit()
                    await s.refresh(row)
            except IntegrityError as exc:
                raise KeyNameError(f"an active key named {name!r} already exists") from exc
            self._active[digest] = Caller(CallerKind.KEY, row.name, row.id)
        return row, key

    async def revoke(self, key_id: int) -> bool:
        async with self._lock:
            async with AsyncSession(self._engine) as s:
                row = await s.get(ApiKeyRow, key_id)
                if row is None or row.revoked_at is not None:
                    return False
                digest = bytes.fromhex(row.key_hash)
                row.revoked_at = _now()
                s.add(row)
                await s.commit()
            self._active.pop(digest, None)
            return True

    async def list_keys(self) -> list[ApiKeyRow]:
        async with AsyncSession(self._engine) as s:
            stmt = select(ApiKeyRow).order_by(
                col(ApiKeyRow.revoked_at).is_not(None), col(ApiKeyRow.created_at).desc()
            )
            return list((await s.exec(stmt)).all())
