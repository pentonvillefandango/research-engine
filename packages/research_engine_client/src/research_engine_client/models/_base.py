"""Base classes shared by every public model."""

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    """Response/data model: strict about unknown fields."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RequestModel(BaseModel):
    """Request model: frozen so it can be hashed into cache keys."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
