"""What a job handler sees."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import BaseModel
from research_engine_client.models import ErrorDetail, EventKind

from research_engine.events.base import Emitter


@dataclass
class JobContext:
    job_id: str
    request: BaseModel
    emitter: Emitter
    _persist_progress: Callable[[int, int, str | None], Awaitable[None]]
    errors: list[ErrorDetail] = field(default_factory=list[ErrorDetail])

    async def progress(self, done: int, total: int, current: str | None = None) -> None:
        """Persist progress and emit ``job.progress``."""
        await self._persist_progress(done, total, current)
        await self.emitter.debug(
            EventKind.JOB_PROGRESS, f"{done}/{total}", done=done, total=total, current=current
        )

    def add_error(self, detail: ErrorDetail) -> None:
        """Record a non-fatal error; a handler that returns with errors ends ``partial``."""
        self.errors.append(detail)


JobHandler = Callable[[JobContext], Awaitable[BaseModel]]
