"""Crossref DOI resolver — maps api.crossref.org/works/{doi} into ZoteroItemDraft."""

from __future__ import annotations

import time
from typing import Any

import httpx

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)

_BASE_URL = "https://api.crossref.org/works/"
_TIMEOUT = 15.0
_RETRY_BACKOFF_S = 1.0

_TYPE_MAP = {
    "journal-article": "journalArticle",
    "book": "book",
    "book-chapter": "bookSection",
    "monograph": "book",
    "edited-book": "book",
    "proceedings-article": "conferencePaper",
    "report": "report",
    "posted-content": "preprint",
}


def resolve(
    doi: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ZoteroItemDraft:
    """Fetch and map a Crossref work record."""
    response = _fetch(doi, transport)
    body = response.json()
    message = body.get("message", {})
    return _map_to_draft(doi, message)


def _fetch(doi: str, transport: httpx.BaseTransport | None) -> httpx.Response:
    url = f"{_BASE_URL}{doi}"
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=_TIMEOUT, transport=transport) as client:
                response = client.get(url)
        except httpx.HTTPError as e:
            last_exc = e
            time.sleep(_RETRY_BACKOFF_S)
            continue

        if response.status_code == 404:
            raise ResolverNotFoundError(f"DOI '{doi}' not found in Crossref")
        if 500 <= response.status_code < 600:
            last_exc = ResolverError(f"Crossref returned {response.status_code} for '{doi}'")
            time.sleep(_RETRY_BACKOFF_S)
            continue
        response.raise_for_status()
        return response

    raise ResolverError(f"Crossref unreachable for '{doi}': {last_exc}")


def _map_to_draft(doi: str, message: dict[str, Any]) -> ZoteroItemDraft:
    title = _first_or_none(message.get("title"))
    if not title:
        raise ResolverError(f"Crossref returned no title for '{doi}'")

    crossref_type = message.get("type", "")
    item_type = _TYPE_MAP.get(crossref_type, "journalArticle")

    fields: dict[str, Any] = {
        "title": title,
        "DOI": message.get("DOI", doi),
    }
    if abstract := message.get("abstract"):
        fields["abstractNote"] = abstract
    if date := _date_from_parts(message):
        fields["date"] = date
    if container := _first_or_none(message.get("container-title")):
        fields["publicationTitle"] = container
    if volume := message.get("volume"):
        fields["volume"] = str(volume)
    if issue := message.get("issue"):
        fields["issue"] = str(issue)
    if pages := message.get("page"):
        fields["pages"] = pages
    if publisher := message.get("publisher"):
        fields["publisher"] = publisher
    if isbn := _first_or_none(message.get("ISBN")):
        fields["ISBN"] = isbn
    if url := message.get("URL"):
        fields["url"] = url

    creators = _map_creators(message.get("author", []))

    return ZoteroItemDraft(
        item_type=item_type,
        fields=fields,
        creators=creators,
        pdf_url=None,
        source_identifier=doi,
    )


def _first_or_none(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    if isinstance(value, str):
        return value
    return None


def _date_from_parts(message: dict[str, Any]) -> str | None:
    issued = (
        message.get("issued") or message.get("published-print") or message.get("published-online")
    )
    if not issued:
        return None
    parts = issued.get("date-parts")
    if not parts or not parts[0]:
        return None
    return "-".join(str(p) for p in parts[0])


def _map_creators(authors: list[dict[str, Any]]) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    for a in authors:
        first = a.get("given", "")
        last = a.get("family", "")
        if not last and (name := a.get("name")):
            creators.append({"creatorType": "author", "name": name})
            continue
        creators.append(
            {
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            }
        )
    return creators
