"""MCP tools for reading and cropping arbitrary local PDF files (not Zotero-managed)."""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

from mcp_local_reference.config import Config
from mcp_local_reference.services.pdf_processor import PdfProcessor


def _resolve_pdf_path(file_path: str, base_dir: Path | None) -> Path:
    """Resolve a user-supplied PDF path, trying base_dir for relative names."""
    p = Path(file_path).expanduser()

    # If not absolute and a base directory is configured, resolve against it
    if not p.is_absolute() and base_dir is not None:
        candidate = (base_dir / p).resolve()
        if candidate.exists():
            p = candidate

    p = p.resolve()
    if not p.exists():
        hint = ""
        if base_dir and not Path(file_path).is_absolute():
            hint = f" (searched in {base_dir})"
        raise ValueError(f"File not found: {p}{hint}")
    if not p.is_file():
        raise ValueError(f"Not a file: {p}")
    if p.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF file: {p}")
    return p


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register tools for reading arbitrary local PDFs."""
    pdf = PdfProcessor(min_figure_pixels=config.min_figure_pixels)
    base_dir = config.local_pdf_dir

    @mcp.tool()
    def read_local_pdf(
        file_path: str,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> str:
        """Extract text from any PDF file on the local machine.

        Use this for PDFs that are NOT managed by Zotero — e.g. files in
        ~/Documents, ~/Downloads, or any other folder.  If LOCAL_PDF_DIR is
        set, relative filenames are resolved against that directory.

        Args:
            file_path: Path to the PDF (absolute, ~-relative, or relative to LOCAL_PDF_DIR).
            start_page: First page to extract (0-indexed, default: first page).
            end_page: Last page to extract (exclusive, default: last page).
        """
        p = _resolve_pdf_path(file_path, base_dir)
        page_count = pdf.get_page_count(p)
        text = pdf.extract_text(p, start_page, end_page)

        if not text.strip():
            return json.dumps(
                {
                    "error": "No text could be extracted (PDF may be image-only)",
                    "page_count": page_count,
                    "file": str(p),
                }
            )
        return text

    @mcp.tool()
    def list_local_figures(file_path: str) -> str:
        """Detect figures and images in any local PDF file.

        Returns a JSON list of detected figures with page numbers, bounding
        boxes, and dimensions.  Pass the results to ``crop_local_figure``
        to extract a specific figure.

        Args:
            file_path: Path to the PDF (absolute, ~-relative, or relative to LOCAL_PDF_DIR).
        """
        p = _resolve_pdf_path(file_path, base_dir)
        figures = pdf.detect_figures(p)

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
    def crop_local_figure(
        file_path: str,
        page: int,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        dpi: int = 300,
    ) -> Image:
        """Crop a rectangular region from any local PDF page as an image.

        Use ``list_local_figures`` first to find figure locations.

        Args:
            file_path: Path to the PDF (absolute, ~-relative, or relative to LOCAL_PDF_DIR).
            page: Page number (0-indexed).
            x0: Left edge of the crop box (PDF points).
            y0: Top edge of the crop box (PDF points).
            x1: Right edge of the crop box (PDF points).
            y1: Bottom edge of the crop box (PDF points).
            dpi: Rendering resolution (default 300).
        """
        p = _resolve_pdf_path(file_path, base_dir)
        png_bytes = pdf.render_page_region(p, page, (x0, y0, x1, y1), dpi)
        return Image(data=png_bytes, format="png")
