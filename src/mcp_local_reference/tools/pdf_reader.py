"""MCP tool for extracting text from reference PDFs."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.services.pdf_processor import PdfProcessor
from mcp_local_reference.services.zotero_client import ZoteroClient


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register PDF text-extraction tools on *mcp*."""
    zotero = ZoteroClient(config)
    pdf = PdfProcessor(min_figure_pixels=config.min_figure_pixels)

    @mcp.tool()
    def get_pdf_text(
        item_key: str,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> str:
        """Extract text from a reference's PDF attachment.

        Args:
            item_key: The Zotero item key.
            start_page: First page to extract (0-indexed, default: first page).
            end_page: Last page to extract (exclusive, default: last page).
        """
        pdf_path = zotero.get_pdf_path(item_key)
        if pdf_path is None:
            return json.dumps({"error": f"No PDF found for reference '{item_key}'"})

        page_count = pdf.get_page_count(pdf_path)
        text = pdf.extract_text(pdf_path, start_page, end_page)

        if not text.strip():
            return json.dumps(
                {
                    "error": "No text could be extracted (PDF may be image-only)",
                    "page_count": page_count,
                }
            )
        return text
