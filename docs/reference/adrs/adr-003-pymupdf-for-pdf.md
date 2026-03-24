# ADR-003: PyMuPDF for PDF Processing

**Status:** Accepted
**Date:** 2025-03-24

## Context

The server needs to extract text from PDFs, detect embedded images with bounding boxes, and render page regions at arbitrary DPI. Options considered: PyMuPDF (fitz), pdf-lib (JS), pdfplumber, pdfminer.

## Decision

Use PyMuPDF (`pymupdf` package, imported as `fitz`) for all PDF operations.

## Consequences

| Aspect | Impact |
|--------|--------|
| Text extraction | `page.get_text()` — handles complex academic layouts |
| Figure detection | `page.get_images()` + `page.get_image_rects()` — returns bounding boxes |
| Page rendering | `page.get_pixmap(clip=rect, dpi=N)` — renders arbitrary regions |
| Single library | One dependency covers text, images, and rendering |
| License | GNU AGPL (pymupdf) — compatible with MIT for server use |
