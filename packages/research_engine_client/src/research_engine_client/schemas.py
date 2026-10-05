"""Render JSON Schemas for every public model."""

import json

from .models import ALL_MODELS, SCHEMA_VERSION
from .models._base import RequestModel


def render_schemas() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, model in sorted(ALL_MODELS.items()):
        # Requests are inputs, so callers may omit defaulted fields: validation mode.
        # Responses always emit every field: serialization mode.
        mode = "validation" if issubclass(model, RequestModel) else "serialization"
        schema = model.model_json_schema(mode=mode)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"research-engine/{SCHEMA_VERSION}/{name}.json"
        schema["title"] = name
        out[name] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return out
