"""PDF text extraction and figure detection using PyMuPDF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


@dataclass
class FigureInfo:
    """Metadata for a figure detected in a PDF page."""

    page_number: int  # 0-indexed
    bbox: tuple[float, float, float, float]  # (x0, y0, x1, y1) in PDF points
    width_px: int
    height_px: int
    image_index: int


class PdfProcessor:
    """Extract text and detect/crop figures from PDFs."""

    def __init__(self, min_figure_pixels: int = 10_000) -> None:
        self.min_figure_pixels = min_figure_pixels

    def extract_text(
        self,
        pdf_path: Path,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> str:
        """Extract text from *pdf_path*, optionally limited to a page range.

        Pages are 0-indexed.  *end_page* is exclusive.
        """
        doc = fitz.open(str(pdf_path))
        try:
            first = start_page or 0
            last = min(end_page or doc.page_count, doc.page_count)
            parts: list[str] = []
            for page_num in range(first, last):
                text = doc[page_num].get_text()
                if text.strip():
                    parts.append(f"--- Page {page_num + 1} ---\n{text}")
            return "\n\n".join(parts)
        finally:
            doc.close()

    def detect_figures(self, pdf_path: Path) -> list[FigureInfo]:
        """Return metadata for every non-trivial image embedded in the PDF."""
        doc = fitz.open(str(pdf_path))
        try:
            figures: list[FigureInfo] = []
            for page_num in range(doc.page_count):
                page = doc[page_num]
                for img_index, img_info in enumerate(page.get_images(full=True)):
                    xref = img_info[0]
                    width = img_info[2]
                    height = img_info[3]

                    if width * height < self.min_figure_pixels:
                        continue

                    rects = page.get_image_rects(xref)
                    if not rects:
                        continue

                    rect = rects[0]
                    figures.append(
                        FigureInfo(
                            page_number=page_num,
                            bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                            width_px=width,
                            height_px=height,
                            image_index=img_index,
                        )
                    )
            return figures
        finally:
            doc.close()

    def render_page_region(
        self,
        pdf_path: Path,
        page_number: int,
        bbox: tuple[float, float, float, float],
        dpi: int = 300,
    ) -> bytes:
        """Render a rectangular region of a page as PNG bytes."""
        doc = fitz.open(str(pdf_path))
        try:
            if page_number < 0 or page_number >= doc.page_count:
                raise ValueError(f"Page {page_number} out of range (0-{doc.page_count - 1})")
            page = doc[page_number]
            clip = fitz.Rect(*bbox)
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            pixmap = page.get_pixmap(matrix=matrix, clip=clip)
            return pixmap.tobytes("png")
        finally:
            doc.close()

    def get_page_count(self, pdf_path: Path) -> int:
        doc = fitz.open(str(pdf_path))
        try:
            return doc.page_count
        finally:
            doc.close()
