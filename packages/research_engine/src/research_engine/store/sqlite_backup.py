"""Online backup and validated restore of the SQLite store (V1-22, §10).

Synchronous ``sqlite3``: these run from the one-shot ``research-engine db`` CLI, never in the
service. Both copy with the SQLite backup API, which takes a consistent snapshot even while the
app writes (WAL readers never block writers), and both produce a self-contained single file.
"""

import contextlib
import os
import sqlite3
from pathlib import Path

REQUIRED_TABLES = ("job", "event", "cache_entry")
BUSY_TIMEOUT_S = 30.0


class DbOpError(Exception):
    """A failed backup or restore. ``code``: 1 the operation failed, 2 a usage error."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _uri(path: Path, params: str) -> str:
    return f"{path.absolute().as_uri()}?{params}"


def _integrity(conn: sqlite3.Connection) -> str:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    return "ok" if rows == [("ok",)] else "; ".join(str(r[0]) for r in rows[:5])


def backup_db(db_path: Path, out: Path) -> int:
    """Copy the live database at ``db_path`` to a new file ``out`` (mode 600). Returns its size.

    ``out`` must not exist. The copy is switched to the rollback journal so it is one complete
    file, then checked with ``PRAGMA integrity_check``. On any failure ``out`` is removed.
    """
    if out.exists() or out.is_symlink():
        raise DbOpError(f"output already exists: {out}", 2)
    if not db_path.is_file():
        raise DbOpError(f"database not found: {db_path}")
    # mode=rw: never create an empty database where the real one is missing
    with contextlib.closing(
        sqlite3.connect(_uri(db_path, "mode=rw"), uri=True, timeout=BUSY_TIMEOUT_S)
    ) as src:
        os.close(os.open(out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        try:
            with contextlib.closing(sqlite3.connect(out)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                result = _integrity(dst)
            if result != "ok":
                raise DbOpError(f"integrity_check failed on the copy: {result}")
        except BaseException:
            out.unlink(missing_ok=True)
            raise
    os.chmod(out, 0o600)
    return out.stat().st_size


def validate_source(src: Path) -> None:
    """Raise unless ``src`` is an intact SQLite database holding the service's tables."""
    if not src.is_file():
        raise DbOpError(f"source not found: {src}", 2)
    # immutable=1: the source may sit on a read-only mount; it is never written
    try:
        with contextlib.closing(sqlite3.connect(_uri(src, "mode=ro&immutable=1"), uri=True)) as c:
            result = _integrity(c)
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as exc:
        raise DbOpError(f"not a usable SQLite database: {exc}") from exc
    if result != "ok":
        raise DbOpError(f"integrity_check failed: {result}")
    missing = [t for t in REQUIRED_TABLES if t not in names]
    if missing:
        raise DbOpError(f"missing tables: {', '.join(missing)}")


def restore_db(src: Path, db_path: Path) -> int:
    """Replace the database at ``db_path`` with ``src`` (validated first). Returns its size.

    The app must be stopped. The copy is built next to ``db_path`` with the backup API, then the
    stale ``-wal``/``-shm``/``-journal`` files are removed *before* the atomic rename: a stale WAL
    left beside the new file would be replayed into it on the next open.
    """
    validate_source(src)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(db_path.name + ".restore-tmp")
    tmp.unlink(missing_ok=True)
    try:
        with (
            contextlib.closing(
                sqlite3.connect(_uri(src, "mode=ro&immutable=1"), uri=True)
            ) as source,
            contextlib.closing(sqlite3.connect(tmp)) as dst,
        ):
            source.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            result = _integrity(dst)
        if result != "ok":
            raise DbOpError(f"integrity_check failed on the restored copy: {result}")
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        os.replace(tmp, db_path)
    finally:
        tmp.unlink(missing_ok=True)
    return db_path.stat().st_size
