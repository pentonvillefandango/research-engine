"""Render JSON Schemas for every public model."""

import json

from .models import ALL_MODELS, SCHEMA_VERSION


def render_schemas() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, model in sorted(ALL_MODELS.items()):
        schema = model.model_json_schema(mode="serialization")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"research-engine/{SCHEMA_VERSION}/{name}.json"
        schema["title"] = name
        out[name] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return out
