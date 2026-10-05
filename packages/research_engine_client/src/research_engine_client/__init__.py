"""Shared Pydantic models and async client for the Research Engine."""

__version__ = "1.0.1"

from .client import ResearchEngineClient, ResearchEngineError

__all__ = ["ResearchEngineClient", "ResearchEngineError", "__version__"]
