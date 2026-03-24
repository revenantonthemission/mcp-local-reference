"""Tests for PDF processing."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_local_reference.services.pdf_processor import PdfProcessor


@pytest.fixture()
def sample_pdf(tmp_dir: Path) -> Path:
    """Create a minimal single-page PDF with text."""
    try:
        import fitz
    except ImportError:
        pytest.skip("PyMuPDF not installed")

    pdf_path = tmp_dir / "test.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This is a test document.\nWith multiple lines of text.")
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


class TestExtractText:
    def test_extracts_text(self, sample_pdf: Path) -> None:
        text = PdfProcessor().extract_text(sample_pdf)
        assert "test document" in text

    def test_page_range(self, sample_pdf: Path) -> None:
        text = PdfProcessor().extract_text(sample_pdf, start_page=0, end_page=1)
        assert "test document" in text

    def test_empty_range_returns_empty(self, sample_pdf: Path) -> None:
        text = PdfProcessor().extract_text(sample_pdf, start_page=5, end_page=6)
        assert text == ""


class TestPageCount:
    def test_single_page(self, sample_pdf: Path) -> None:
        assert PdfProcessor().get_page_count(sample_pdf) == 1


class TestRenderPageRegion:
    def test_returns_png_bytes(self, sample_pdf: Path) -> None:
        png = PdfProcessor().render_page_region(sample_pdf, 0, (0, 0, 200, 200), dpi=72)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_invalid_page_raises(self, sample_pdf: Path) -> None:
        with pytest.raises(ValueError, match="out of range"):
            PdfProcessor().render_page_region(sample_pdf, 99, (0, 0, 100, 100))
