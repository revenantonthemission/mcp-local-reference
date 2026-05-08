"""Tests for the add_reference tool module."""

from __future__ import annotations

import httpx

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
