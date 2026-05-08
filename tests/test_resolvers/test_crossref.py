"""Tests for the Crossref DOI resolver."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
    crossref,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "resolvers" / "crossref"


def _transport(status: int, body: bytes | str) -> httpx.MockTransport:
    if isinstance(body, str):
        body_bytes = body.encode()
    else:
        body_bytes = body

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body_bytes)

    return httpx.MockTransport(handler)


def test_resolve_journal_article_maps_fields():
    body = (FIXTURES / "journal_article.json").read_bytes()
    transport = _transport(200, body)

    draft = crossref.resolve("10.1038/nature14539", transport=transport)

    assert isinstance(draft, ZoteroItemDraft)
    assert draft.item_type == "journalArticle"
    assert draft.fields["title"]
    assert draft.fields["DOI"] == "10.1038/nature14539"
    assert len(draft.creators) >= 1
    assert all(c["creatorType"] == "author" for c in draft.creators)
    assert draft.pdf_url is None
    assert draft.source_identifier == "10.1038/nature14539"


def test_resolve_book_chapter_uses_book_section_type():
    body = (FIXTURES / "book_chapter.json").read_bytes()
    transport = _transport(200, body)

    draft = crossref.resolve("10.1007/978-3-030-01234-2_5", transport=transport)

    assert draft.item_type == "bookSection"


def test_resolve_404_raises_not_found():
    transport = _transport(404, b'{"status":"error","message":[{"value":"Resource not found."}]}')

    with pytest.raises(ResolverNotFoundError) as exc:
        crossref.resolve("10.0000/does-not-exist", transport=transport)
    assert "10.0000/does-not-exist" in str(exc.value)


def test_resolve_5xx_retries_once_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, content=b"upstream down")

    transport = httpx.MockTransport(handler)
    with pytest.raises(ResolverError):
        crossref.resolve("10.1145/anything", transport=transport)
    assert calls["n"] == 2  # original + 1 retry


def test_resolve_malformed_response_missing_title_raises():
    transport = _transport(200, b'{"message": {"DOI": "10.1145/x", "type": "journal-article"}}')

    with pytest.raises(ResolverError) as exc:
        crossref.resolve("10.1145/x", transport=transport)
    assert "title" in str(exc.value).lower()
