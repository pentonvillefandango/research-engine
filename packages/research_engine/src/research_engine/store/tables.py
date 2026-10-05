"""SQLite tables (D9). JSON payloads are stored as text, validated via the public models on read.

Timestamps are ``NaiveDatetime`` (naive UTC): this SQLModel version maps a bare ``datetime`` to an
aware-only ``UTCDateTime`` column, and SQLite drops tzinfo anyway. Converters re-attach ``UTC``.
"""

from pydantic import NaiveDatetime
from sqlalchemy import Column, Index, LargeBinary
from sqlmodel import Field, SQLModel


class JobRow(SQLModel, table=True):
    __tablename__ = "job"  # type: ignore[assignment]  # SQLModel types __tablename__ as declared_attr
    __table_args__ = (Index("ix_job_status_created", "status", "created_at"),)
    id: str = Field(primary_key=True)
    type: str
    status: str
    progress_done: int = 0
    progress_total: int = 0
    progress_current: str | None = None
    parent_id: str | None = None
    session_id: str | None = Field(default=None, index=True)
    request_json: str
    result_json: str | None = None
    errors_json: str = "[]"
    created_at: NaiveDatetime
    started_at: NaiveDatetime | None = None
    finished_at: NaiveDatetime | None = None


class EventRow(SQLModel, table=True):
    __tablename__ = "event"  # type: ignore[assignment]  # SQLModel types __tablename__ as declared_attr
    __table_args__ = (
        Index("ix_event_ts", "ts"),
        Index("ix_event_job_id", "job_id"),
        Index("ix_event_kind", "kind"),
    )
    id: int | None = Field(default=None, primary_key=True)
    ts: NaiveDatetime
    job_id: str | None = None
    level: str
    kind: str
    message: str
    data_json: str = "{}"


class CacheRow(SQLModel, table=True):
    __tablename__ = "cache_entry"  # type: ignore[assignment]  # SQLModel types __tablename__ as declared_attr
    key: str = Field(primary_key=True)
    value: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    expires_at: float = Field(index=True)
