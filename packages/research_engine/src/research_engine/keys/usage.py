"""Request log (V1-24). Task 6 fills this in."""

from sqlalchemy.ext.asyncio import AsyncEngine


class UsageLog:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
