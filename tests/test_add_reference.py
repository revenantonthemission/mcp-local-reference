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


def _draft(doi: str = "10.1234/x", item_type: str = "journalArticle") -> ZoteroItemDraft:
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
        "10.1234/x",
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
    resolver.assert_called_once_with("10.1234/x")
    zotero_api.create_item.assert_not_called()


def test_doi_dry_run_exists_returns_existing_key():
    from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl

    zotero = MagicMock()
    zotero.find_by_doi.return_value = "OLD123"
    zotero_api = MagicMock()
    resolver = MagicMock(return_value=_draft())

    result_json = add_reference_by_doi_impl(
        "10.1234/x",
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
        "10.1234/x",
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
        "10.1234/x",
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
        "10.1234/x",
        collection_key=None,
        dry_run=False,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
    )
    result = json.loads(result_json)
    assert result["status"] == "error"
    assert "creds" in result["error"].lower()


def _arxiv_draft(arxiv_id: str = "2401.12345") -> ZoteroItemDraft:
    return ZoteroItemDraft(
        item_type="preprint",
        fields={"title": "Preprint Title", "extra": f"arXiv:{arxiv_id}"},
        creators=[{"creatorType": "author", "firstName": "A", "lastName": "B"}],
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
        source_identifier=arxiv_id,
    )


def test_arxiv_live_create_with_pdf_attached():
    from mcp_local_reference.tools.add_reference import add_reference_by_arxiv_impl

    zotero = MagicMock()
    zotero.find_by_arxiv_id.return_value = None
    zotero_api = MagicMock()
    zotero_api.create_item.return_value = ItemSnapshot(
        item_key="NEW789",
        version=1,
        tags=[],
        collections=[],
        raw={},
    )
    zotero_api.upload_attachment.return_value = "ATTACH01"
    resolver = MagicMock(return_value=_arxiv_draft())
    pdf_fetcher = MagicMock(return_value=(b"%PDF-1.7\n" + b"x" * 2000, "ok"))

    result_json = add_reference_by_arxiv_impl(
        "2401.12345",
        collection_key=None,
        dry_run=False,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
        pdf_fetcher=pdf_fetcher,
        max_pdf_mb=50,
    )
    result = json.loads(result_json)
    assert result["status"] == "created"
    assert result["pdf_status"] == "attached"
    zotero_api.upload_attachment.assert_called_once()


def test_arxiv_pdf_failed_does_not_roll_back_item():
    from mcp_local_reference.tools.add_reference import add_reference_by_arxiv_impl

    zotero = MagicMock()
    zotero.find_by_arxiv_id.return_value = None
    zotero_api = MagicMock()
    zotero_api.create_item.return_value = ItemSnapshot(
        item_key="NEW999",
        version=1,
        tags=[],
        collections=[],
        raw={},
    )
    resolver = MagicMock(return_value=_arxiv_draft())
    pdf_fetcher = MagicMock(return_value=(None, "failed"))

    result_json = add_reference_by_arxiv_impl(
        "2401.12345",
        collection_key=None,
        dry_run=False,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
        pdf_fetcher=pdf_fetcher,
        max_pdf_mb=50,
    )
    result = json.loads(result_json)
    assert result["status"] == "created"
    assert result["item_key"] == "NEW999"
    assert result["pdf_status"] == "failed"
    zotero_api.upload_attachment.assert_not_called()


def test_arxiv_pdf_upload_exception_downgrades_to_failed():
    from mcp_local_reference.services.zotero_api_client import ZoteroApiError
    from mcp_local_reference.tools.add_reference import add_reference_by_arxiv_impl

    zotero = MagicMock()
    zotero.find_by_arxiv_id.return_value = None
    zotero_api = MagicMock()
    zotero_api.create_item.return_value = ItemSnapshot(
        item_key="NEW000",
        version=1,
        tags=[],
        collections=[],
        raw={},
    )
    zotero_api.upload_attachment.side_effect = ZoteroApiError("S3 down")
    resolver = MagicMock(return_value=_arxiv_draft())
    pdf_fetcher = MagicMock(return_value=(b"%PDF-1.7\n" + b"x" * 2000, "ok"))

    result_json = add_reference_by_arxiv_impl(
        "2401.12345",
        collection_key=None,
        dry_run=False,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
        pdf_fetcher=pdf_fetcher,
        max_pdf_mb=50,
    )
    result = json.loads(result_json)
    assert result["status"] == "created"  # NOT rolled back
    assert result["pdf_status"] == "failed"


def test_arxiv_invalid_id_format_returns_error():
    from mcp_local_reference.tools.add_reference import add_reference_by_arxiv_impl

    result_json = add_reference_by_arxiv_impl(
        "not-an-arxiv-id",
        collection_key=None,
        dry_run=True,
        zotero=MagicMock(),
        zotero_api=MagicMock(),
        resolver=MagicMock(),
        pdf_fetcher=MagicMock(),
        max_pdf_mb=50,
    )
    result = json.loads(result_json)
    assert result["status"] == "error"


def test_arxiv_dry_run_pdf_status_skipped():
    from mcp_local_reference.tools.add_reference import add_reference_by_arxiv_impl

    zotero = MagicMock()
    zotero.find_by_arxiv_id.return_value = None
    zotero_api = MagicMock()
    resolver = MagicMock(return_value=_arxiv_draft())
    pdf_fetcher = MagicMock()

    result_json = add_reference_by_arxiv_impl(
        "2401.12345",
        collection_key=None,
        dry_run=True,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
        pdf_fetcher=pdf_fetcher,
        max_pdf_mb=50,
    )
    result = json.loads(result_json)
    assert result["status"] == "would_create"
    assert result["pdf_status"] == "skipped"
    pdf_fetcher.assert_not_called()
    zotero_api.upload_attachment.assert_not_called()


def test_isbn_invalid_checksum_returns_error():
    from mcp_local_reference.tools.add_reference import add_reference_by_isbn_impl

    result_json = add_reference_by_isbn_impl(
        "9781234567890",
        collection_key=None,
        dry_run=True,  # bad ISBN-13 checksum
        zotero=MagicMock(),
        zotero_api=MagicMock(),
        resolver=MagicMock(),
    )
    result = json.loads(result_json)
    assert result["status"] == "error"
    assert "isbn" in result["error"].lower()


def test_isbn_dry_run_not_exists():
    from mcp_local_reference.tools.add_reference import add_reference_by_isbn_impl

    zotero = MagicMock()
    zotero.find_by_isbn.return_value = None
    zotero_api = MagicMock()
    book_draft = ZoteroItemDraft(
        item_type="book",
        fields={"title": "A Book", "ISBN": "9780674042070"},
        creators=[],
        pdf_url=None,
        source_identifier="9780674042070",
    )
    resolver = MagicMock(return_value=book_draft)

    result_json = add_reference_by_isbn_impl(
        "9780674042070",
        collection_key=None,
        dry_run=True,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=resolver,
    )
    result = json.loads(result_json)
    assert result["status"] == "would_create"
    assert result["item_type"] == "book"
    assert result["pdf_status"] is None


def test_isbn_dry_run_exists():
    from mcp_local_reference.tools.add_reference import add_reference_by_isbn_impl

    zotero = MagicMock()
    zotero.find_by_isbn.return_value = "ISBNITEM1"
    zotero_api = MagicMock()
    book_draft = ZoteroItemDraft(
        item_type="book",
        fields={"title": "T", "ISBN": "9780674042070"},
        creators=[],
        pdf_url=None,
        source_identifier="9780674042070",
    )

    result_json = add_reference_by_isbn_impl(
        "978-0-674-04207-0",
        collection_key=None,
        dry_run=True,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=MagicMock(return_value=book_draft),
    )
    result = json.loads(result_json)
    assert result["status"] == "exists"
    assert result["item_key"] == "ISBNITEM1"


def test_isbn10_with_x_check_digit_accepted():
    from mcp_local_reference.tools.add_reference import add_reference_by_isbn_impl

    # 043942089X is a real valid ISBN-10 (Harry Potter UK first ed.)
    # checksum: 10*0+9*4+8*3+7*9+6*4+5*2+4*0+3*8+2*9+1*10 = 209, 209 % 11 == 0
    zotero = MagicMock()
    zotero.find_by_isbn.return_value = None
    zotero_api = MagicMock()
    book_draft = ZoteroItemDraft(
        item_type="book",
        fields={"title": "T", "ISBN": "043942089X"},
        creators=[],
        pdf_url=None,
        source_identifier="043942089X",
    )

    result_json = add_reference_by_isbn_impl(
        "0-43-942089-X",
        collection_key=None,
        dry_run=True,
        zotero=zotero,
        zotero_api=zotero_api,
        resolver=MagicMock(return_value=book_draft),
    )
    result = json.loads(result_json)
    assert result["status"] != "error"
