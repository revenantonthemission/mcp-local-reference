"""MCP tools for detecting and cropping figures from reference PDFs."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP, Image

from mcp_local_reference.config import Config
from mcp_local_reference.services.pdf_processor import PdfProcessor
from mcp_local_reference.services.zotero_client import ZoteroClient


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register figure-related tools on *mcp*."""
    zotero = ZoteroClient(config)
    pdf = PdfProcessor(min_figure_pixels=config.min_figure_pixels)

    @mcp.tool()
    def list_figures(item_key: str) -> str:
        """Detect figures and images embedded in a reference's PDF.

        Returns a JSON list with each figure's page number, bounding box
        (in PDF points), and pixel dimensions.  Pass the bbox values to
        ``crop_figure`` to extract a specific figure as an image.

        Args:
            item_key: The Zotero item key.
        """
        pdf_path = zotero.get_pdf_path(item_key)
        if pdf_path is None:
            return json.dumps({"error": f"No PDF found for reference '{item_key}'"})

        figures = pdf.detect_figures(pdf_path)

        return json.dumps(
            [
                {
                    "index": i,
                    "page": fig.page_number,
                    "bbox": {
                        "x0": round(fig.bbox[0], 1),
                        "y0": round(fig.bbox[1], 1),
                        "x1": round(fig.bbox[2], 1),
                        "y1": round(fig.bbox[3], 1),
                    },
                    "size": {"width": fig.width_px, "height": fig.height_px},
                }
                for i, fig in enumerate(figures)
            ],
            indent=2,
        )

    @mcp.tool()
    def crop_figure(
        item_key: str,
        page: int,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        dpi: int = 300,
    ) -> Image:
        """Crop a rectangular region from a PDF page and return it as an image.

        Use ``list_figures`` first to discover figure locations, then call
        this tool with the bounding-box coordinates.

        Args:
            item_key: The Zotero item key.
            page: Page number (0-indexed).
            x0: Left edge of the crop box (PDF points).
            y0: Top edge of the crop box (PDF points).
            x1: Right edge of the crop box (PDF points).
            y1: Bottom edge of the crop box (PDF points).
            dpi: Rendering resolution (default 300).
        """
        pdf_path = zotero.get_pdf_path(item_key)
        if pdf_path is None:
            raise ValueError(f"No PDF found for reference '{item_key}'")

        png_bytes = pdf.render_page_region(pdf_path, page, (x0, y0, x1, y1), dpi)
        return Image(data=png_bytes, format="png")
