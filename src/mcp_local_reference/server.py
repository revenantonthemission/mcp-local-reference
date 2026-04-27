"""MCP server creation and tool registration."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.tools import (
    auto_tag,
    figures,
    local_pdf,
    pdf_reader,
    references,
)
from mcp_local_reference.tools import (
    collections as collections_tools,
)


def create_server() -> FastMCP:
    """Build a fully-configured FastMCP server with all tools registered."""
    config = Config()
    mcp = FastMCP("mcp-local-reference")

    references.register_tools(mcp, config)
    pdf_reader.register_tools(mcp, config)
    figures.register_tools(mcp, config)
    local_pdf.register_tools(mcp, config)
    auto_tag.register_tools(mcp, config)
    collections_tools.register_tools(mcp, config)

    return mcp
