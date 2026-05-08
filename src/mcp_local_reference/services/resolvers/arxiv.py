"""arXiv resolver — maps export.arxiv.org Atom feed into ZoteroItemDraft."""

from __future__ import annotations

import re
import time
from xml.etree import ElementTree as ET

import httpx

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)

_BASE_URL = "http://export.arxiv.org/api/query"
_TIMEOUT = 15.0
_RETRY_BACKOFF_S = 1.0
_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
_VERSION_SUFFIX_RE = re.compile(r"v\d+$")


def resolve(
    arxiv_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ZoteroItemDraft:
    """Fetch and map an arXiv preprint."""
    raw_xml = _fetch(arxiv_id, transport)
    return _map_to_draft(arxiv_id, raw_xml)


def _fetch(arxiv_id: str, transport: httpx.BaseTransport | None) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=_TIMEOUT, transport=transport) as client:
                response = client.get(_BASE_URL, params={"id_list": arxiv_id})
        except httpx.HTTPError as e:
            last_exc = e
            time.sleep(_RETRY_BACKOFF_S)
            continue

        if 500 <= response.status_code < 600:
            last_exc = ResolverError(f"arXiv returned {response.status_code} for '{arxiv_id}'")
            time.sleep(_RETRY_BACKOFF_S)
            continue
        response.raise_for_status()
        return response.content

    raise ResolverError(f"arXiv unreachable for '{arxiv_id}': {last_exc}")


def _map_to_draft(arxiv_id: str, raw_xml: bytes) -> ZoteroItemDraft:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as e:
        raise ResolverError(f"arXiv returned malformed XML: {e}") from e

    entries = root.findall("a:entry", _NS)
    if not entries:
        raise ResolverNotFoundError(f"arXiv ID '{arxiv_id}' not found")
    entry = entries[0]

    title_el = entry.find("a:title", _NS)
    if title_el is None or not (title_el.text or "").strip():
        raise ResolverNotFoundError(f"arXiv ID '{arxiv_id}' not found")

    title_text = (title_el.text or "").strip()
    if title_text.lower() == "error":
        raise ResolverNotFoundError(f"arXiv ID '{arxiv_id}' not found")

    summary_el = entry.find("a:summary", _NS)
    published_el = entry.find("a:published", _NS)

    bare_id = _VERSION_SUFFIX_RE.sub("", arxiv_id)

    fields: dict[str, str] = {
        "title": title_text,
        "extra": f"arXiv:{bare_id}",
        "url": f"https://arxiv.org/abs/{arxiv_id}",
    }
    if summary_el is not None and summary_el.text:
        fields["abstractNote"] = summary_el.text.strip()
    if published_el is not None and published_el.text:
        fields["date"] = published_el.text[:10]

    creators: list[dict[str, str]] = []
    for author_el in entry.findall("a:author", _NS):
        name_el = author_el.find("a:name", _NS)
        if name_el is None or not name_el.text:
            continue
        parts = name_el.text.rsplit(" ", 1)
        if len(parts) == 2:
            first, last = parts
        else:
            first, last = "", parts[0]
        creators.append(
            {
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            }
        )

    return ZoteroItemDraft(
        item_type="preprint",
        fields=fields,
        creators=creators,
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
        source_identifier=arxiv_id,
    )
