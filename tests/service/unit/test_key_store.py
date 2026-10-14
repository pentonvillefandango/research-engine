import hashlib
import re

import pytest
from research_engine.keys.models import Caller, CallerKind
from research_engine.keys.store import KeyNameError, KeyStore
from research_engine.store.db import create_engine_for, init_db
from sqlalchemy import text

ENV_KEY = "env-key-" + "9" * 32


@pytest.fixture
async def store():
    engine = create_engine_for(":memory:")
    await init_db(engine)
    s = KeyStore(engine, ENV_KEY.encode())
    await s.load()
    yield s
    await engine.dispose()


async def test_create_stores_hash_only(store: KeyStore) -> None:
    row, key = await store.create("newsdesk")
    assert re.fullmatch(r"re_[A-Za-z0-9_-]{43}", key)
    assert row.prefix == key[3:11]
    assert row.key_hash == hashlib.sha256(key.encode()).hexdigest()
    async with store.engine.connect() as conn:
        dump = (await conn.execute(text("SELECT * FROM api_key"))).all()
    assert key not in repr(dump)


async def test_match_and_revoke(store: KeyStore) -> None:
    row, key = await store.create("newsdesk")
    assert row.id is not None
    assert store.match(key.encode()) == Caller(CallerKind.KEY, "newsdesk", row.id)
    assert await store.revoke(row.id) is True
    assert store.match(key.encode()) is None
    assert await store.revoke(row.id) is False
    assert await store.revoke(999) is False


async def test_env_key_and_empty(store: KeyStore) -> None:
    assert store.match(ENV_KEY.encode()) == Caller(CallerKind.ENV, "env (built-in)", None)
    assert store.match(b"") is None
    assert store.match(b"re_nope") is None


async def test_names(store: KeyStore) -> None:
    row, _ = await store.create("app")
    assert row.id is not None
    with pytest.raises(KeyNameError):
        await store.create("app")
    await store.revoke(row.id)
    await store.create("app")  # reusable once revoked
    for bad in ("", "   ", "x" * 65):
        with pytest.raises(KeyNameError):
            await store.create(bad)


async def test_load_restores_active_keys(store: KeyStore) -> None:
    _, key = await store.create("a")
    fresh = KeyStore(store.engine, ENV_KEY.encode())
    await fresh.load()
    assert fresh.match(key.encode()) is not None


async def test_indexes_exist(store: KeyStore) -> None:
    async with store.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))).all()
    names = {r[0] for r in rows}
    assert {"ux_api_key_active_name", "ix_api_request_key_ts", "ix_api_request_ts"} <= names


async def test_revoke_and_create_do_not_reload(
    store: KeyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom() -> None:
        raise RuntimeError("load must not be called")

    monkeypatch.setattr(store, "load", boom)
    row, key = await store.create("a")
    assert store.match(key.encode()) is not None
    assert row.id is not None
    assert await store.revoke(row.id) is True
    assert store.match(key.encode()) is None


async def test_duplicate_name_leaves_map_unchanged(store: KeyStore) -> None:
    _, key = await store.create("dup")
    before = dict(store._active)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(KeyNameError):
        await store.create("dup")
    assert store._active == before  # pyright: ignore[reportPrivateUsage]
    assert store.match(key.encode()) is not None


async def test_list_keys_order(store: KeyStore) -> None:
    first, _ = await store.create("first")
    await store.create("second")
    await store.create("third")
    assert first.id is not None
    await store.revoke(first.id)
    rows = await store.list_keys()
    assert [r.name for r in rows] == ["third", "second", "first"]


async def test_nonprintable_name_rejected(store: KeyStore) -> None:
    with pytest.raises(KeyNameError):
        await store.create("a\x00b")
