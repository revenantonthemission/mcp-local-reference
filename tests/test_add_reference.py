"""Tests for the add_reference tool module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx

from mcp_local_reference.services.resolvers import (
    ResolverNotFoundError,
    ZoteroItemDraft,
)
from mcp_local_reference.services.zotero_api_client import (
    ItemSnapshot,
    MissingCredentialsError,
)
from mcp_local_reference.tools.add_reference import _fetch_pdf


def _transport(
    status: int, body: bytes, headers: dict[str, str] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers or {})

    return httpx.MockTransport(handler)


def test_fetch_pdf_valid_magic_bytes_returns_ok():
    body = b"%PDF-1.7\n" + b"x" * 2000
    pdf, status = _fetch_pdf("https://example/p.pdf", 50, transport=_transport(200, body))
    assert status == "ok"
    assert pdf == body


def test_fetch_pdf_html_error_page_rejected():
    body = b"<!DOCTYPE html><html><body>arXiv error</body></html>"
    pdf, status = _fetch_pdf("https://example/p.pdf", 50, transport=_transport(200, body))
    assert status == "failed"
    assert pdf is None


def test_fetch_pdf_below_min_size_rejected():
    body = b"%PDF-1.7\n" + b"x" * 100  # well under 1KB
    pdf, status = _fetch_pdf("https://example/p.pdf", 50, transport=_transport(200, body))
    assert status == "failed"


def test_fetch_pdf_oversize_via_content_length_skipped():
    body = b""
    headers = {"Content-Length": str(60 * 1024 * 1024)}
    pdf, status = _fetch_pdf("https://example/p.pdf", 50, transport=_transport(200, body, headers))
    assert status == "skipped"
    assert pdf is None


def test_fetch_pdf_404_returns_failed():
    pdf, status = _fetch_pdf("https://example/p.pdf", 50, transport=_transport(404, b""))
    assert status == "failed"


def test_fetch_pdf_network_error_returns_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failure")

    pdf, status = _fetch_pdf("https://example/p.pdf", 50, transport=httpx.MockTransport(handler))
    assert status == "failed"


def _draft(doi: str = "10.1/x", item_type: str = "journalArticle") -> ZoteroItemDraft:
    return ZoteroItemDraft(
        item_type=item_type,
        fields={"title": "Resolved Title", "DOI": doi},
        creators=[{"creatorType": "author", "firstName": "A", "lastName": "B"}],
        pdf_url=None,
        source_identifier=doi,
    )


def test_doi_invalid_format_returns_error():
    from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl

    result_json = add_reference_by_doi_impl(
        "not-a-doi",
        collection_key=None,
        dry_run=True,
        zotero=MagicMock(),
        zotero_api=MagicMock(),
        resolver=MagicMock(),
    )
    result = json.loads(result_json)
    assert result["status"] == "error"
    assert "Invalid DOI" in result["error"]


def test_doi_dry_run_not_exists_calls_resolver_not_create():
    from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl

    zotero = MagicMock()
    zotero.find_by_doi.return_value = None
    zotero_api = MagicMock()
    resolver = MagicMock(return_value=_draft())

    result_json = add_reference_by_doi_impl(
        "10.1/x",
        collection_key=None,
        dry_run=True,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
    )
    result = json.loads(result_json)
    assert result["status"] == "would_create"
    assert result["title"] == "Resolved Title"
    assert result["dry_run"] is True
    resolver.assert_called_once_with("10.1/x")
    zotero_api.create_item.assert_not_called()


def test_doi_dry_run_exists_returns_existing_key():
    from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl

    zotero = MagicMock()
    zotero.find_by_doi.return_value = "OLD123"
    zotero_api = MagicMock()
    resolver = MagicMock(return_value=_draft())

    result_json = add_reference_by_doi_impl(
        "10.1/x",
        collection_key=None,
        dry_run=True,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
    )
    result = json.loads(result_json)
    assert result["status"] == "exists"
    assert result["item_key"] == "OLD123"
    zotero_api.create_item.assert_not_called()


def test_doi_live_create_calls_create_item_and_returns_new_key():
    from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl

    zotero = MagicMock()
    zotero.find_by_doi.return_value = None
    zotero_api = MagicMock()
    zotero_api.create_item.return_value = ItemSnapshot(
        item_key="NEW456",
        version=1,
        tags=[],
        collections=[],
        raw={},
    )
    resolver = MagicMock(return_value=_draft())

    result_json = add_reference_by_doi_impl(
        "10.1/x",
        collection_key="ABC",
        dry_run=False,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
    )
    result = json.loads(result_json)
    assert result["status"] == "created"
    assert result["item_key"] == "NEW456"
    assert result["pdf_status"] is None  # DOI tool: never attaches a PDF
    zotero_api.create_item.assert_called_once()
    _, kwargs = zotero_api.create_item.call_args
    assert kwargs["collection_key"] == "ABC"


def test_doi_resolver_not_found_returns_error():
    from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl

    zotero = MagicMock()
    zotero_api = MagicMock()
    resolver = MagicMock(side_effect=ResolverNotFoundError("DOI not found"))

    result_json = add_reference_by_doi_impl(
        "10.1/x",
        collection_key=None,
        dry_run=True,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
    )
    result = json.loads(result_json)
    assert result["status"] == "error"
    assert "not found" in result["error"].lower()


def test_doi_missing_credentials_propagates():
    from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl

    zotero = MagicMock()
    zotero.find_by_doi.return_value = None
    zotero_api = MagicMock()
    zotero_api.create_item.side_effect = MissingCredentialsError("creds unset")
    resolver = MagicMock(return_value=_draft())

    result_json = add_reference_by_doi_impl(
        "10.1/x",
        collection_key=None,
        dry_run=False,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
    )
    result = json.loads(result_json)
    assert result["status"] == "error"
    assert "creds" in result["error"].lower()
