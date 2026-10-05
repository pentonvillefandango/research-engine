"""MCP server (V1-12): the REST service layer exposed as MCP tools at ``/mcp``."""

from .server import INSTRUCTIONS, MCP_PATHS, build_mcp, mount_mcp

__all__ = ["INSTRUCTIONS", "MCP_PATHS", "build_mcp", "mount_mcp"]
