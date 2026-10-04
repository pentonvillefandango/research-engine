"""Base classes shared by every public model."""

from datetime import UTC
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict

UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda d: d.astimezone(UTC))]
"""Timezone-aware datetime, normalised to UTC on validation."""


class Model(BaseModel):
    """Response/data model: strict about unknown fields.

    Defaulted fields are still listed as `required` in the serialisation JSON Schema, because
    responses always emit them (optional fields are explicit nulls, never omitted).
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_serialization_defaults_required=True,
    )


class RequestModel(BaseModel):
    """Request model: frozen so it can be hashed into cache keys."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
