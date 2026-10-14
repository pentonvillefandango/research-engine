"""Who made a request (V1-24)."""

from dataclasses import dataclass
from enum import StrEnum


class CallerKind(StrEnum):
    KEY = "key"
    ENV = "env"
    GUI = "gui"


@dataclass(frozen=True, slots=True)
class Caller:
    kind: CallerKind
    name: str
    key_id: int | None


ENV_CALLER = Caller(CallerKind.ENV, "env (built-in)", None)
GUI_CALLER = Caller(CallerKind.GUI, "GUI session", None)
