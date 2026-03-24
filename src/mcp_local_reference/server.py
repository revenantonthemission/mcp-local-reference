"""MCP server creation and tool registration."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.tools import figures, pdf_reader, references


def create_server() -> FastMCP:
    """Build a fully-configured FastMCP server with all tools registered."""
    config = Config()
    mcp = FastMCP("mcp-local-reference")

    references.register_tools(mcp, config)
    pdf_reader.register_tools(mcp, config)
    figures.register_tools(mcp, config)

    return mcp
