"""MCP tools for adding references to Zotero by identifier (DOI, arXiv, ISBN).

Resolves citation metadata via external APIs (Crossref, arXiv, Open Library),
deduplicates against the local Zotero SQLite, and POSTs new bibliographic
items to ``api.zotero.org``. The arXiv tool also auto-attaches the
open-access PDF.

Tool implementations live in module-level ``*_impl()`` helpers so they're
unit-testable without an MCP harness.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

import httpx

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)
from mcp_local_reference.services.resolvers import crossref as crossref_resolver
from mcp_local_reference.services.zotero_api_client import (
    MissingCredentialsError,
    ZoteroApiClient,
    ZoteroApiError,
)
from mcp_local_reference.services.zotero_client import ZoteroClient

logger = logging.getLogger(__name__)

_DOI_RE = re.compile(r"^10\.\d+/[^\s]+$")

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


def add_reference_by_doi_impl(
    doi: str,
    collection_key: str | None,
    dry_run: bool,
    *,
    zotero: ZoteroClient,
    zotero_api: ZoteroApiClient,
    resolver: Callable[[str], ZoteroItemDraft] = crossref_resolver.resolve,
) -> str:
    """Resolve, dedup, and (if not dry-run and not duplicate) POST a DOI item.

    Returns a JSON string. Shape documented in the spec under 'Response Shape'.
    """
    if not _DOI_RE.fullmatch(doi):
        return json.dumps({"status": "error", "error": f"Invalid DOI format: {doi}"})

    try:
        draft = resolver(doi)
    except ResolverNotFoundError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except ResolverError as e:
        return json.dumps({"status": "error", "error": str(e)})

    existing = zotero.find_by_doi(doi)
    if existing is not None:
        return json.dumps(
            {
                "status": "exists",
                "item_key": existing,
                "title": draft.fields.get("title"),
                "warning": f"Already in library as {existing}",
                "dry_run": dry_run,
            }
        )

    if dry_run:
        return json.dumps(
            {
                "status": "would_create",
                "title": draft.fields.get("title"),
                "item_type": draft.item_type,
                "collection_key": collection_key,
                "pdf_status": None,
                "dry_run": True,
            }
        )

    try:
        snapshot = zotero_api.create_item(draft, collection_key=collection_key)
    except (MissingCredentialsError, ZoteroApiError) as e:
        return json.dumps({"status": "error", "error": str(e)})

    return json.dumps(
        {
            "status": "created",
            "item_key": snapshot.item_key,
            "title": draft.fields.get("title"),
            "item_type": draft.item_type,
            "collection_key": collection_key,
            "pdf_status": None,
            "dry_run": False,
        }
    )
