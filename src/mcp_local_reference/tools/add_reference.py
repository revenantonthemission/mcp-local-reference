"""MCP tools for adding references to Zotero by identifier (DOI, arXiv, ISBN).

Resolves citation metadata via external APIs (Crossref, arXiv, Open Library),
deduplicates against the local Zotero SQLite, and POSTs new bibliographic
items to ``api.zotero.org``. The arXiv tool also auto-attaches the
open-access PDF.

Tool implementations live in module-level ``*_impl()`` helpers so they're
unit-testable without an MCP harness.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"
_PDF_MIN_SIZE_BYTES = 1024
_PDF_DOWNLOAD_TIMEOUT_S = 30.0


def _fetch_pdf(
    url: str,
    max_size_mb: int,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bytes | None, str]:
    """Fetch a PDF, returning (bytes_or_None, status).

    Status values:
      - "ok": valid PDF bytes returned
      - "skipped": Content-Length exceeded max_size_mb (no body downloaded)
      - "failed": 404, network error, magic-byte check failed, or below min size

    The caller logs the failure reason; the helper itself logs at INFO.
    """
    max_bytes = max_size_mb * 1024 * 1024
    try:
        with httpx.Client(
            timeout=_PDF_DOWNLOAD_TIMEOUT_S, transport=transport, follow_redirects=True
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 404:
                    logger.info("PDF 404: %s", url)
                    return None, "failed"
                if response.status_code >= 300:
                    logger.info("PDF non-200 (%s): %s", response.status_code, url)
                    return None, "failed"

                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    logger.info("PDF oversize (%s bytes): %s", content_length, url)
                    return None, "skipped"

                buf = bytearray()
                for chunk in response.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.info("PDF stream exceeded max size: %s", url)
                        return None, "skipped"

                pdf_bytes = bytes(buf)
    except httpx.HTTPError as e:
        logger.info("PDF fetch error for %s: %s", url, e)
        return None, "failed"

    if len(pdf_bytes) < _PDF_MIN_SIZE_BYTES:
        logger.info("PDF too small (%d bytes): %s", len(pdf_bytes), url)
        return None, "failed"
    if not pdf_bytes.startswith(_PDF_MAGIC):
        logger.info("PDF magic-byte check failed (got %r...): %s", pdf_bytes[:16], url)
        return None, "failed"

    return pdf_bytes, "ok"
