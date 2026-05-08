"""Identifier resolvers — fetch citation metadata from external APIs.

Each resolver module exports a single ``resolve(identifier: str) -> ZoteroItemDraft``
function. They are stateless, raise ``ResolverError`` on failure, and never
talk to Zotero (the caller does that)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ZoteroItemDraft:
    """Resolver output — Zotero-shaped but not yet POSTed."""

    item_type: str
    """One of 'journalArticle', 'preprint', 'book', 'bookSection'."""

    fields: dict[str, Any]
    """title, abstractNote, date, DOI, ISBN, url, publisher, extra, ..."""

    creators: list[dict[str, str]]
    """[{'creatorType': 'author', 'firstName': 'Ada', 'lastName': 'Lovelace'}, ...]"""

    pdf_url: str | None
    """Set only by the arxiv resolver."""

    source_identifier: str
    """The identifier the caller passed in — echoed back for traceability."""


class ResolverError(RuntimeError):
    """Generic resolver failure — network, malformed response, etc."""


class ResolverNotFoundError(ResolverError):
    """Identifier not found in upstream metadata API (HTTP 404)."""
