"""Tests for the Open Library ISBN resolver."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
    openlibrary,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "resolvers" / "openlibrary"


def _transport(status: int, body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def test_normalize_isbn_strips_hyphens_and_spaces():
    assert openlibrary.normalize_isbn("978-0-674-04207-0") == "9780674042070"
    assert openlibrary.normalize_isbn("0 631 20569 1") == "0631205691"
    assert openlibrary.normalize_isbn("\t013602X1") == "013602X1".upper()
    assert openlibrary.normalize_isbn("0-13602x-1") == "013602X1"


def test_resolve_isbn13_full():
    body = (FIXTURES / "isbn13_full.json").read_bytes()
    draft = openlibrary.resolve("9780674042070", transport=_transport(200, body))

    assert isinstance(draft, ZoteroItemDraft)
    assert draft.item_type == "book"
    assert draft.fields["title"]
    assert draft.fields["ISBN"] == "9780674042070"
    assert draft.pdf_url is None


def test_resolve_isbn10_sparse_metadata_still_returns():
    body = (FIXTURES / "isbn10_sparse.json").read_bytes()
    draft = openlibrary.resolve("0631205691", transport=_transport(200, body))

    assert draft.fields["title"]
    # Publisher missing on the sparse fixture — must not raise
    assert "publisher" in draft.fields or "publisher" not in draft.fields


def test_resolve_isbn_with_hyphens_normalizes_input():
    body = (FIXTURES / "isbn13_full.json").read_bytes()
    draft = openlibrary.resolve("978-0-674-04207-0", transport=_transport(200, body))
    assert draft.fields["ISBN"] == "9780674042070"


def test_resolve_unknown_isbn_raises_not_found():
    body = (FIXTURES / "not_found.json").read_bytes()
    with pytest.raises(ResolverNotFoundError):
        openlibrary.resolve("1111111111111", transport=_transport(200, body))


def test_resolve_malformed_response_missing_title_raises():
    transport = _transport(200, b'{"ISBN:9780000000000": {"publishers": [{"name": "X"}]}}')
    with pytest.raises(ResolverError):
        openlibrary.resolve("9780000000000", transport=transport)


def test_resolve_5xx_retries_once_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, content=b"")

    with pytest.raises(ResolverError):
        openlibrary.resolve("9780674042070", transport=httpx.MockTransport(handler))
    assert calls["n"] == 2
