"""`research-engine db backup|restore` (V1-22, §10): online SQLite backup and validated restore."""

import asyncio
import contextlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest
from research_engine.cli import main
from research_engine.store.db import create_engine_for, init_db


async def _create_schema(path: Path) -> None:
    engine = create_engine_for(str(path))
    try:
        await init_db(engine)
    finally:
        await engine.dispose()


def make_db(path: Path, marker: str = "j1") -> None:
    """A database with the real schema (WAL, as the app runs it) and one job row."""
    asyncio.run(_create_schema(path))
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO job (id, type, status, progress_done, progress_total, request_json,"
            " errors_json, created_at) VALUES (?, 'fetch', 'done', 0, 0, '{}', '[]',"
            " '2026-10-05 00:00:00')",
            (marker,),
        )


def job_ids(path: Path) -> list[str]:
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM job ORDER BY id")]


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main(["db", *argv])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, lines
    return code, json.loads(lines[0])


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data" / "research-engine.sqlite"
    monkeypatch.setenv("DB_PATH", str(path))
    return path


# ---- backup ----------------------------------------------------------------------------------


def test_backup_copies_db_checks_integrity_and_prints_result(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(db)
    out = tmp_path / "out" / ".backup-1.sqlite"
    out.parent.mkdir()
    code, res = run(capsys, "backup", "--out", str(out))
    assert code == 0
    assert res["ok"] is True and res["path"] == str(out) and res["bytes"] == out.stat().st_size
    assert job_ids(out) == ["j1"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600  # job data: owner only
    # a self-contained single file (rollback journal), so copying just it out is complete
    with contextlib.closing(sqlite3.connect(out)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not Path(f"{out}-wal").exists()


def test_backup_is_safe_while_a_writer_holds_a_transaction(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(db)
    writer = sqlite3.connect(db, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO job (id, type, status, request_json, errors_json, created_at,"
            " progress_done, progress_total) VALUES ('uncommitted', 'fetch', 'queued', '{}',"
            " '[]', '2026-10-05 00:00:00', 0, 0)"
        )
        out = tmp_path / "b.sqlite"
        code, res = run(capsys, "backup", "--out", str(out))
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert code == 0 and res["ok"] is True
    assert job_ids(out) == ["j1"]  # committed data only


def test_backup_refuses_existing_out(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(db)
    out = tmp_path / "exists.sqlite"
    out.write_bytes(b"keep me")
    code, res = run(capsys, "backup", "--out", str(out))
    assert code == 2 and res["ok"] is False and "exists" in str(res["error"])
    assert out.read_bytes() == b"keep me"


def test_backup_missing_database_fails_without_creating_anything(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "b.sqlite"
    code, res = run(capsys, "backup", "--out", str(out))
    assert code == 1 and res["ok"] is False
    assert not out.exists() and not db.exists()


# ---- restore ---------------------------------------------------------------------------------


def test_restore_replaces_db_and_removes_stale_wal_and_shm(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(db, "current")
    src = tmp_path / "src.sqlite"
    make_db(src, "restored")
    src.chmod(0o444)  # mounted read-only in the container
    for suffix in ("-wal", "-shm"):
        Path(f"{db}{suffix}").write_bytes(b"stale")
    code, res = run(capsys, "restore", "--from", str(src))
    assert code == 0, res
    assert res["ok"] is True and res["restored"] == str(db)
    assert job_ids(db) == ["restored"]
    assert not Path(f"{db}-wal").exists() and not Path(f"{db}-shm").exists()
    assert not list(db.parent.glob("*.restore-tmp*"))
    with contextlib.closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restore_into_fresh_volume(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "src.sqlite"
    make_db(src, "restored")
    code, res = run(capsys, "restore", "--from", str(src))
    assert code == 0 and res["ok"] is True
    assert job_ids(db) == ["restored"]


def test_restore_round_trips_a_backup(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(db, "before")
    out = tmp_path / "b.sqlite"
    assert run(capsys, "backup", "--out", str(out))[0] == 0
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE job SET id = 'after'")
    assert run(capsys, "restore", "--from", str(out))[0] == 0
    assert job_ids(db) == ["before"]


def test_restore_rejects_missing_tables_and_leaves_db_untouched(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(db, "current")
    src = tmp_path / "other.sqlite"
    with contextlib.closing(sqlite3.connect(src)) as conn, conn:
        conn.execute("CREATE TABLE job (id TEXT)")
    code, res = run(capsys, "restore", "--from", str(src))
    assert code == 1 and res["ok"] is False
    assert "cache_entry" in str(res["error"]) and "event" in str(res["error"])
    assert job_ids(db) == ["current"]


def test_restore_rejects_a_non_database(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(db, "current")
    src = tmp_path / "junk.sqlite"
    src.write_bytes(b"not a database at all" * 100)
    code, res = run(capsys, "restore", "--from", str(src))
    assert code == 1 and res["ok"] is False
    assert job_ids(db) == ["current"]


def test_restore_missing_source_is_a_usage_error(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, res = run(capsys, "restore", "--from", str(tmp_path / "nope.sqlite"))
    assert code == 2 and res["ok"] is False
    assert not db.exists()


def test_restore_check_validates_without_touching_the_db(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(db, "current")
    src = tmp_path / "src.sqlite"
    make_db(src, "restored")
    before = db.read_bytes()
    code, res = run(capsys, "restore", "--check", "--from", str(src))
    assert code == 0 and res["ok"] is True and res["checked"] is True
    assert db.read_bytes() == before and job_ids(db) == ["current"]
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"junk" * 100)
    code, res = run(capsys, "restore", "--check", "--from", str(bad))
    assert code == 1 and res["ok"] is False and res["replaced"] is False


def test_restore_failure_reports_the_db_was_not_replaced(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(db, "current")
    src = tmp_path / "junk.sqlite"
    src.write_bytes(b"junk" * 100)
    code, res = run(capsys, "restore", "--from", str(src))
    assert code == 1 and res["replaced"] is False


def test_restore_refuses_a_wal_source_with_its_wal_file(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raw copy of a live WAL database: committed rows still live in the -wal file, which an
    immutable read would silently ignore. Refused with a clear message."""
    live = tmp_path / "live" / "x.sqlite"
    live.parent.mkdir()
    make_db(live, "base")
    holder = sqlite3.connect(live)  # keeps the WAL from being checkpointed away on close
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        with holder:
            holder.execute("UPDATE job SET id = 'in-wal'")
        assert Path(f"{live}-wal").stat().st_size > 0
        code, res = run(capsys, "restore", "--from", str(live))
    finally:
        holder.close()
    assert code == 1 and res["ok"] is False and "-wal" in str(res["error"])
    assert not db.exists()
