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
from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)
from mcp_local_reference.services.resolvers import arxiv as arxiv_resolver
from mcp_local_reference.services.resolvers import crossref as crossref_resolver
from mcp_local_reference.services.resolvers import openlibrary as openlibrary_resolver
from mcp_local_reference.services.zotero_api_client import (
    MissingCredentialsError,
    ZoteroApiClient,
    ZoteroApiError,
)
from mcp_local_reference.services.zotero_client import ZoteroClient

logger = logging.getLogger(__name__)

_DOI_RE = re.compile(r"^10\.\d{4,9}/[^\s]+$")

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


_ARXIV_NEW_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_ARXIV_OLD_RE = re.compile(r"^[a-z\-]+/\d{7}$")


def _is_valid_arxiv_id(arxiv_id: str) -> bool:
    return bool(_ARXIV_NEW_RE.fullmatch(arxiv_id) or _ARXIV_OLD_RE.fullmatch(arxiv_id))


def add_reference_by_arxiv_impl(
    arxiv_id: str,
    collection_key: str | None,
    dry_run: bool,
    *,
    zotero: ZoteroClient,
    zotero_api: ZoteroApiClient,
    resolver: Callable[[str], ZoteroItemDraft] = arxiv_resolver.resolve,
    pdf_fetcher: Callable[[str, int], tuple[bytes | None, str]] = _fetch_pdf,
    max_pdf_mb: int = 50,
) -> str:
    if not _is_valid_arxiv_id(arxiv_id):
        return json.dumps({"status": "error", "error": f"Invalid arXiv ID format: {arxiv_id}"})

    try:
        draft = resolver(arxiv_id)
    except ResolverNotFoundError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except ResolverError as e:
        return json.dumps({"status": "error", "error": str(e)})

    existing = zotero.find_by_arxiv_id(arxiv_id)
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
                "pdf_status": "skipped",
                "dry_run": True,
            }
        )

    try:
        snapshot = zotero_api.create_item(draft, collection_key=collection_key)
    except (MissingCredentialsError, ZoteroApiError) as e:
        return json.dumps({"status": "error", "error": str(e)})

    pdf_status = "skipped"
    if draft.pdf_url:
        pdf_bytes, fetch_status = pdf_fetcher(draft.pdf_url, max_pdf_mb)
        if fetch_status == "ok" and pdf_bytes is not None:
            try:
                zotero_api.upload_attachment(
                    parent_key=snapshot.item_key,
                    pdf_bytes=pdf_bytes,
                    filename=f"{arxiv_id.replace('/', '_')}.pdf",
                )
                pdf_status = "attached"
            except ZoteroApiError as e:
                logger.warning("PDF upload failed for %s: %s", snapshot.item_key, e)
                pdf_status = "failed"
        else:
            pdf_status = fetch_status  # "failed" or "skipped"

    return json.dumps(
        {
            "status": "created",
            "item_key": snapshot.item_key,
            "title": draft.fields.get("title"),
            "item_type": draft.item_type,
            "collection_key": collection_key,
            "pdf_status": pdf_status,
            "dry_run": False,
        }
    )


def _is_valid_isbn(raw: str) -> bool:
    """Validate ISBN-10 or ISBN-13 checksum after normalization."""
    isbn = openlibrary_resolver.normalize_isbn(raw)
    if len(isbn) == 10:
        return _isbn10_checksum(isbn)
    if len(isbn) == 13:
        return _isbn13_checksum(isbn)
    return False


def _isbn10_checksum(isbn: str) -> bool:
    if not all(c.isdigit() or (i == 9 and c == "X") for i, c in enumerate(isbn)):
        return False
    total = 0
    for i, c in enumerate(isbn):
        v = 10 if c == "X" else int(c)
        total += v * (10 - i)
    return total % 11 == 0


def _isbn13_checksum(isbn: str) -> bool:
    if not isbn.isdigit():
        return False
    total = 0
    for i, c in enumerate(isbn):
        v = int(c)
        total += v if i % 2 == 0 else v * 3
    return total % 10 == 0


def add_reference_by_isbn_impl(
    isbn: str,
    collection_key: str | None,
    dry_run: bool,
    *,
    zotero: ZoteroClient,
    zotero_api: ZoteroApiClient,
    resolver: Callable[[str], ZoteroItemDraft] = openlibrary_resolver.resolve,
) -> str:
    """Resolve, dedup, and (if not dry-run and not duplicate) POST an ISBN item.

    Returns a JSON string. Shape documented in the spec under 'Response Shape'.
    """
    if not _is_valid_isbn(isbn):
        return json.dumps({"status": "error", "error": f"Invalid ISBN format/checksum: {isbn}"})

    try:
        draft = resolver(isbn)
    except ResolverNotFoundError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except ResolverError as e:
        return json.dumps({"status": "error", "error": str(e)})

    existing = zotero.find_by_isbn(isbn)
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


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register the three add_reference_by_* MCP tools."""
    zotero = ZoteroClient(config)
    zotero_api = ZoteroApiClient(config)

    @mcp.tool()
    def add_reference_by_doi(
        doi: str,
        collection_key: str | None = None,
        dry_run: bool = True,
    ) -> str:
        """Add a reference to Zotero by DOI.

        Resolves metadata via Crossref, deduplicates against the local library,
        and POSTs a new item if not duplicate.

        Args:
            doi: The DOI string (e.g., '10.1145/3458817.3476195').
            collection_key: Optional Zotero collection key to file the new item in.
            dry_run: If True (default), preview the resolved metadata without writing.
        """
        return add_reference_by_doi_impl(
            doi,
            collection_key=collection_key,
            dry_run=dry_run,
            zotero=zotero,
            zotero_api=zotero_api,
        )

    @mcp.tool()
    def add_reference_by_arxiv(
        arxiv_id: str,
        collection_key: str | None = None,
        dry_run: bool = True,
    ) -> str:
        """Add a reference to Zotero by arXiv ID.

        Resolves metadata via the arXiv API, deduplicates, POSTs the item, and
        attempts to attach the open-access PDF as a child item (non-fatal if
        the PDF can't be fetched).

        Args:
            arxiv_id: The arXiv identifier (e.g., '2401.12345' or 'hep-th/0211177').
                Optional 'vN' version suffix preserved on the PDF URL.
            collection_key: Optional Zotero collection key.
            dry_run: If True (default), preview without writing.
        """
        return add_reference_by_arxiv_impl(
            arxiv_id,
            collection_key=collection_key,
            dry_run=dry_run,
            zotero=zotero,
            zotero_api=zotero_api,
            max_pdf_mb=config.add_reference_max_pdf_mb,
        )

    @mcp.tool()
    def add_reference_by_isbn(
        isbn: str,
        collection_key: str | None = None,
        dry_run: bool = True,
    ) -> str:
        """Add a reference to Zotero by ISBN.

        Resolves metadata via Open Library, deduplicates, POSTs a new book item.

        Args:
            isbn: ISBN-10 or ISBN-13, hyphenated or not. Checksum validated.
            collection_key: Optional Zotero collection key.
            dry_run: If True (default), preview without writing.
        """
        return add_reference_by_isbn_impl(
            isbn,
            collection_key=collection_key,
            dry_run=dry_run,
            zotero=zotero,
            zotero_api=zotero_api,
        )
