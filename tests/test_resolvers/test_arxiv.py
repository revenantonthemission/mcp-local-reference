"""Tests for the arXiv resolver."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
    arxiv,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "resolvers" / "arxiv"


def _transport(status: int, body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def test_resolve_new_style_id_maps_fields():
    body = (FIXTURES / "2401_12345.xml").read_bytes()
    draft = arxiv.resolve("2401.12345", transport=_transport(200, body))

    assert isinstance(draft, ZoteroItemDraft)
    assert draft.item_type == "preprint"
    assert draft.fields["title"]
    assert draft.fields.get("extra", "").startswith("arXiv:2401.12345")
    assert draft.pdf_url == "https://arxiv.org/pdf/2401.12345.pdf"
    assert len(draft.creators) >= 1


def test_resolve_old_style_id_maps_fields():
    body = (FIXTURES / "hep-th_0211177.xml").read_bytes()
    draft = arxiv.resolve("hep-th/0211177", transport=_transport(200, body))

    assert draft.pdf_url == "https://arxiv.org/pdf/hep-th/0211177.pdf"
    assert "arXiv:hep-th/0211177" in draft.fields["extra"]


def test_resolve_strips_version_suffix():
    body = (FIXTURES / "2401_12345.xml").read_bytes()
    draft = arxiv.resolve("2401.12345v3", transport=_transport(200, body))

    # PDF URL keeps the v-suffix so we get the exact requested version
    assert draft.pdf_url == "https://arxiv.org/pdf/2401.12345v3.pdf"
    # but extra/source_identifier records what user passed
    assert draft.source_identifier == "2401.12345v3"


def test_resolve_unknown_id_raises_not_found():
    body = (FIXTURES / "withdrawn.xml").read_bytes()
    with pytest.raises(ResolverNotFoundError):
        arxiv.resolve("9999.99999", transport=_transport(200, body))


def test_resolve_5xx_retries_once_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, content=b"down")

    with pytest.raises(ResolverError):
        arxiv.resolve("2401.12345", transport=httpx.MockTransport(handler))
    assert calls["n"] == 2


def test_resolve_malformed_xml_raises():
    with pytest.raises(ResolverError):
        arxiv.resolve("2401.12345", transport=_transport(200, b"<<<not xml"))
