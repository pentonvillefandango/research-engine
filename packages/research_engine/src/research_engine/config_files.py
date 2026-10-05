"""Loaders for YAML config files: intent presets and the test-console demos."""

from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from research_engine_client.models import (
    FetchRequest,
    IntentPreset,
    SearchIntent,
    SearchReadRequest,
    SearchRequest,
)


class _IntentsFile(BaseModel):
    intents: dict[SearchIntent, IntentPreset]


class IntentRegistry:
    def __init__(self, presets: dict[SearchIntent, IntentPreset]) -> None:
        missing = set(SearchIntent) - set(presets)
        if missing:
            raise ValueError(
                f"intents file missing presets for: {sorted(m.value for m in missing)}"
            )
        self._presets = presets

    @classmethod
    def load(cls, path: Path | str) -> Self:
        raw = yaml.safe_load(Path(path).read_text())
        try:
            parsed = _IntentsFile.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"invalid intents file {path}: {exc}") from exc
        return cls(parsed.intents)

    def get(self, intent: SearchIntent) -> IntentPreset:
        return self._presets[intent]

    def all(self) -> dict[SearchIntent, IntentPreset]:
        return dict(self._presets)


DemoKind = Literal["search", "fetch", "search_read"]
DEMO_REQUEST_MODELS: dict[str, type[BaseModel]] = {
    "search": SearchRequest,
    "fetch": FetchRequest,
    "search_read": SearchReadRequest,
}


class Demo(BaseModel):
    """One test-console demo (V1-21): ``request`` must validate against ``kind``'s model.

    ``request`` keeps the file's own (minimal) fields so the console fills only those and
    leaves every other form field at its default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    kind: DemoKind
    request: dict[str, Any]

    @model_validator(mode="after")
    def _request_matches_kind(self) -> Self:
        DEMO_REQUEST_MODELS[self.kind].model_validate(self.request)
        return self


class _DemosFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    demos: list[Demo]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [d.id for d in self.demos]
        if len(set(ids)) != len(ids):
            raise ValueError("demo ids must be unique")
        return self


def load_demos(path: Path | str) -> list[Demo]:
    """Parse and validate the demos file; ``ValueError`` names the file on any problem."""
    try:
        raw = yaml.safe_load(Path(path).read_text())
        return _DemosFile.model_validate(raw).demos
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"invalid demos file {path}: {exc}") from exc
