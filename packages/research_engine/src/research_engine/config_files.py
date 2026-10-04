"""Loaders for YAML config files (intent presets; demos in step 6)."""

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ValidationError
from research_engine_client.models import IntentPreset, SearchIntent


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
