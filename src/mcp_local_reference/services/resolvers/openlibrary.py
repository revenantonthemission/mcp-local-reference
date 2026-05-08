"""Open Library ISBN resolver — maps openlibrary.org/api/books into ZoteroItemDraft."""

from __future__ import annotations

import time
from typing import Any

import httpx

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)

_BASE_URL = "https://openlibrary.org/api/books"
_TIMEOUT = 15.0
_RETRY_BACKOFF_S = 1.0


def normalize_isbn(isbn: str) -> str:
    """Strip hyphens, whitespace; uppercase trailing X."""
    return "".join(ch.upper() if ch in "xX" else ch for ch in isbn if ch.isalnum())


def resolve(
    isbn: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ZoteroItemDraft:
    """Fetch and map an Open Library book record."""
    normalized = normalize_isbn(isbn)
    body = _fetch(normalized, transport)
    record = body.get(f"ISBN:{normalized}")
    if not record:
        raise ResolverNotFoundError(f"ISBN '{normalized}' not found in Open Library")
    return _map_to_draft(normalized, record)


def _fetch(normalized: str, transport: httpx.BaseTransport | None) -> dict[str, Any]:
    last_exc: Exception | None = None
    params = {"bibkeys": f"ISBN:{normalized}", "format": "json", "jscmd": "data"}
    for attempt in range(2):
        try:
            with httpx.Client(timeout=_TIMEOUT, transport=transport) as client:
                response = client.get(_BASE_URL, params=params)
        except httpx.HTTPError as e:
            last_exc = e
            time.sleep(_RETRY_BACKOFF_S)
            continue

        if 500 <= response.status_code < 600:
            last_exc = ResolverError(
                f"Open Library returned {response.status_code} for ISBN '{normalized}'"
            )
            time.sleep(_RETRY_BACKOFF_S)
            continue
        response.raise_for_status()
        return response.json()

    raise ResolverError(f"Open Library unreachable for ISBN '{normalized}': {last_exc}")


def _map_to_draft(normalized: str, record: dict[str, Any]) -> ZoteroItemDraft:
    title = record.get("title")
    if not title:
        raise ResolverError(f"Open Library returned no title for ISBN '{normalized}'")

    fields: dict[str, Any] = {
        "title": title,
        "ISBN": normalized,
    }
    if publish_date := record.get("publish_date"):
        fields["date"] = publish_date
    if publishers := record.get("publishers"):
        names = [p.get("name") for p in publishers if p.get("name")]
        if names:
            fields["publisher"] = ", ".join(names)
    if places := record.get("publish_places"):
        names = [p.get("name") for p in places if p.get("name")]
        if names:
            fields["place"] = names[0]
    if url := record.get("url"):
        fields["url"] = url
    if num_pages := record.get("number_of_pages"):
        fields["numPages"] = str(num_pages)

    creators: list[dict[str, str]] = []
    for a in record.get("authors", []):
        full_name = a.get("name", "")
        if not full_name:
            continue
        if " " in full_name:
            first, _, last = full_name.rpartition(" ")
        else:
            first, last = "", full_name
        creators.append(
            {
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            }
        )

    return ZoteroItemDraft(
        item_type="book",
        fields=fields,
        creators=creators,
        pdf_url=None,
        source_identifier=normalized,
    )
