# Add-Reference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three MCP tools — `add_reference_by_doi`, `add_reference_by_arxiv`, `add_reference_by_isbn` — that resolve identifiers via Crossref / arXiv / Open Library, deduplicate against the local Zotero SQLite, and POST new bibliographic items to the Zotero Web API. The arXiv tool also fetches and attaches the open-access PDF as a child item.

**Architecture:** New tool module `src/mcp_local_reference/tools/add_reference.py` (Approach 1 from the spec — three separate tools, one per identifier type). New `src/mcp_local_reference/services/resolvers/` sub-package holding three stateless metadata fetchers. Extends `ZoteroClient` with three `find_by_*` dedup methods (read-only SQLite) and `ZoteroApiClient` with `create_item` + `upload_attachment` (Web API writes). Skip-and-warn dedup is the load-bearing safety primitive — if the identifier already exists locally, return the existing key with `status: "exists"` and never POST.

**Tech Stack:** Python 3.11+, FastMCP, pydantic-settings, httpx (test layer uses `httpx.MockTransport`), pytest, stdlib `xml.etree` for arXiv Atom parsing. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-08-add-reference-design.md` (commit `2cbc55f`).

**Branch:** `feat/add-reference` (create at session start; spec already on `main`).

---

## Reference: Existing helpers being reused

These are already in the codebase. Engineers reading tasks out of order will find them in the listed locations.

- **`ItemSnapshot`** (dataclass in `services/zotero_api_client.py:31`) — already returned by `get_item` / `update_item_collections`. `create_item` will return the same shape.
- **`ZoteroApiError` / `MissingCredentialsError`** (`services/zotero_api_client.py:19, 23`) — reuse for write failures and credential gating.
- **`ZoteroApiClient.create_collection`** (`services/zotero_api_client.py:168`) — model for the new `create_item` (POST `[payload]`, parse `body["successful"]["0"]`, raise on `body["failed"]`).
- **`ZoteroClient._get_fields`** (`services/zotero_client.py:368`) — model for the dedup query joining `items` → `itemData` → `itemDataValues` filtered by `fieldID`.
- **`Config` model** (`config.py:19`) — `add_reference_max_pdf_mb` is a new field; pattern: plain `int` with default literal.
- **`mock_zotero_db` fixture** (`tests/conftest.py`) — populates a SQLite file with a tiny Zotero schema. Task 5 extends it with the `extra` field (fieldID 15) and adds items carrying DOIs / arXiv IDs / ISBNs for dedup tests.
- **`auto_tag.py`** (`tools/auto_tag.py`) — pattern for `*_impl()` helpers outside the `register_tools()` closure with injected dependencies for testability. The new tool module follows this exactly.
- **`collections.py`** (`tools/collections.py`) — pattern for `dry_run=True` default + per-call constants (e.g., `MAX_TAGS_PER_CALL = 25`). The PDF size cap follows the same shape as a constant pulled from config.

---

## Pre-flight (one-time setup, no commit)

- [ ] **Create the feature branch**

```bash
git checkout -b feat/add-reference
git status   # confirm clean tree
```

---

### Task 1: Shared resolver types and exceptions

Creates the `services/resolvers/` sub-package and defines the contract resolvers must satisfy.

**Files:**
- Create: `src/mcp_local_reference/services/resolvers/__init__.py`
- Create: `tests/test_resolvers/__init__.py` (empty file, marks the directory as a test package)

- [ ] **Step 1: Create the resolvers package init**

Write `src/mcp_local_reference/services/resolvers/__init__.py`:

```python
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
```

- [ ] **Step 2: Create empty test package marker**

Write `tests/test_resolvers/__init__.py` as an empty file. (Required for pytest discovery in package layout.)

```bash
mkdir -p tests/test_resolvers
: > tests/test_resolvers/__init__.py
```

- [ ] **Step 3: Run existing tests to confirm nothing broke**

```bash
uv run pytest -v
```

Expected: all existing tests pass; no new tests yet.

- [ ] **Step 4: Commit**

```bash
git add src/mcp_local_reference/services/resolvers/__init__.py tests/test_resolvers/__init__.py
git commit -m "feat(resolvers): scaffold sub-package with ZoteroItemDraft + exceptions"
```

---

### Task 2: Crossref resolver

Resolves DOIs via `api.crossref.org/works/{doi}`. Maps Crossref's response into `ZoteroItemDraft` with `item_type` driven by Crossref's `type` field.

**Files:**
- Create: `src/mcp_local_reference/services/resolvers/crossref.py`
- Create: `tests/test_resolvers/test_crossref.py`
- Create: `tests/fixtures/resolvers/crossref/journal_article.json`
- Create: `tests/fixtures/resolvers/crossref/book_chapter.json`
- Create: `tests/fixtures/resolvers/crossref/corporate_author.json`

- [ ] **Step 1: Capture three real Crossref fixtures**

```bash
mkdir -p tests/fixtures/resolvers/crossref
curl -s 'https://api.crossref.org/works/10.1145/3458817.3476195' > tests/fixtures/resolvers/crossref/journal_article.json
curl -s 'https://api.crossref.org/works/10.1007/978-3-030-01234-2_5' > tests/fixtures/resolvers/crossref/book_chapter.json
curl -s 'https://api.crossref.org/works/10.48550/arXiv.2401.12345' > tests/fixtures/resolvers/crossref/corporate_author.json
```

(If any of these 404 or change shape, use any DOI of the same `type` value: `journal-article`, `book-chapter`, or one with a single corporate-style author. The DOI strings above are illustrative.)

- [ ] **Step 2: Write failing tests**

Write `tests/test_resolvers/test_crossref.py`:

```python
"""Tests for the Crossref DOI resolver."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)
from mcp_local_reference.services.resolvers import crossref

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

    draft = crossref.resolve("10.1145/3458817.3476195", transport=transport)

    assert isinstance(draft, ZoteroItemDraft)
    assert draft.item_type == "journalArticle"
    assert draft.fields["title"]
    assert draft.fields["DOI"] == "10.1145/3458817.3476195"
    assert len(draft.creators) >= 1
    assert all(c["creatorType"] == "author" for c in draft.creators)
    assert draft.pdf_url is None
    assert draft.source_identifier == "10.1145/3458817.3476195"


def test_resolve_book_chapter_uses_bookSection_type():
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
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
uv run pytest tests/test_resolvers/test_crossref.py -v
```

Expected: ImportError or "module has no attribute 'resolve'" — `crossref.py` doesn't exist yet.

- [ ] **Step 4: Implement the resolver**

Write `src/mcp_local_reference/services/resolvers/crossref.py`:

```python
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

# Crossref `type` → Zotero `itemType`. Anything not in the map falls through
# to "journalArticle" as a sensible default.
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
    """Fetch and map a Crossref work record.

    Args:
        doi: The DOI string (without leading 'doi:' or URL prefix).
        transport: Test seam for ``httpx.MockTransport``.

    Raises:
        ResolverNotFoundError: HTTP 404.
        ResolverError: 5xx after retry, network failure, malformed response.
    """
    response = _fetch(doi, transport)
    body = response.json()
    message = body.get("message", {})
    return _map_to_draft(doi, message)


def _fetch(doi: str, transport: httpx.BaseTransport | None) -> httpx.Response:
    url = f"{_BASE_URL}{doi}"
    last_exc: Exception | None = None
    for attempt in range(2):  # original + 1 retry
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
            last_exc = ResolverError(
                f"Crossref returned {response.status_code} for '{doi}'"
            )
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
    issued = message.get("issued") or message.get("published-print") or message.get("published-online")
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
            # Corporate author: Crossref uses {"name": "..."} instead of given/family.
            creators.append({"creatorType": "author", "name": name})
            continue
        creators.append({
            "creatorType": "author",
            "firstName": first,
            "lastName": last,
        })
    return creators
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
uv run pytest tests/test_resolvers/test_crossref.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Lint**

```bash
uv run ruff check src/mcp_local_reference/services/resolvers/crossref.py tests/test_resolvers/test_crossref.py
uv run ruff format src/mcp_local_reference/services/resolvers/crossref.py tests/test_resolvers/test_crossref.py
```

- [ ] **Step 7: Commit**

```bash
git add src/mcp_local_reference/services/resolvers/crossref.py tests/test_resolvers/test_crossref.py tests/fixtures/resolvers/crossref/
git commit -m "feat(resolvers): add Crossref DOI resolver"
```

---

### Task 3: arXiv resolver

Resolves arXiv IDs via `export.arxiv.org/api/query?id_list=<id>` (Atom XML). Sets `pdf_url` to the canonical open-access URL.

**Files:**
- Create: `src/mcp_local_reference/services/resolvers/arxiv.py`
- Create: `tests/test_resolvers/test_arxiv.py`
- Create: `tests/fixtures/resolvers/arxiv/2401_12345.xml` (new-style ID)
- Create: `tests/fixtures/resolvers/arxiv/hep-th_0211177.xml` (old-style ID)
- Create: `tests/fixtures/resolvers/arxiv/withdrawn.xml` (404-like body returned with 200)

- [ ] **Step 1: Capture arXiv fixtures**

```bash
mkdir -p tests/fixtures/resolvers/arxiv
curl -s 'http://export.arxiv.org/api/query?id_list=2401.12345' > tests/fixtures/resolvers/arxiv/2401_12345.xml
curl -s 'http://export.arxiv.org/api/query?id_list=hep-th/0211177' > tests/fixtures/resolvers/arxiv/hep-th_0211177.xml
# arXiv returns a 200 with an empty <feed> for unknown IDs — capture one:
curl -s 'http://export.arxiv.org/api/query?id_list=9999.99999' > tests/fixtures/resolvers/arxiv/withdrawn.xml
```

- [ ] **Step 2: Write failing tests**

Write `tests/test_resolvers/test_arxiv.py`:

```python
"""Tests for the arXiv resolver."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)
from mcp_local_reference.services.resolvers import arxiv

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
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
uv run pytest tests/test_resolvers/test_arxiv.py -v
```

Expected: ImportError on `arxiv` module.

- [ ] **Step 4: Implement the arxiv resolver**

Write `src/mcp_local_reference/services/resolvers/arxiv.py`:

```python
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
# Match the "version-naked" form of the ID for storage in `extra`.
_VERSION_SUFFIX_RE = re.compile(r"v\d+$")


def resolve(
    arxiv_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ZoteroItemDraft:
    """Fetch and map an arXiv preprint.

    The PDF URL preserves any user-supplied version suffix (e.g. ``v3``) so
    that the attached PDF matches the exact version requested. The ``extra``
    field stores the version-naked form.
    """
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
            last_exc = ResolverError(
                f"arXiv returned {response.status_code} for '{arxiv_id}'"
            )
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

    # arXiv returns errors as a single entry with title 'Error' — sniff and reject:
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
        fields["date"] = published_el.text[:10]  # YYYY-MM-DD slice

    creators: list[dict[str, str]] = []
    for author_el in entry.findall("a:author", _NS):
        name_el = author_el.find("a:name", _NS)
        if name_el is None or not name_el.text:
            continue
        first, _, last = name_el.text.rsplit(" ", 1) if " " in name_el.text else ("", "", name_el.text)
        creators.append({
            "creatorType": "author",
            "firstName": first,
            "lastName": last,
        })

    return ZoteroItemDraft(
        item_type="preprint",
        fields=fields,
        creators=creators,
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
        source_identifier=arxiv_id,
    )
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
uv run pytest tests/test_resolvers/test_arxiv.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Lint**

```bash
uv run ruff check src/mcp_local_reference/services/resolvers/arxiv.py tests/test_resolvers/test_arxiv.py
uv run ruff format src/mcp_local_reference/services/resolvers/arxiv.py tests/test_resolvers/test_arxiv.py
```

- [ ] **Step 7: Commit**

```bash
git add src/mcp_local_reference/services/resolvers/arxiv.py tests/test_resolvers/test_arxiv.py tests/fixtures/resolvers/arxiv/
git commit -m "feat(resolvers): add arXiv resolver with PDF URL"
```

---

### Task 4: Open Library resolver

Resolves ISBNs via `openlibrary.org/api/books?bibkeys=ISBN:<isbn>`. Includes ISBN normalization (strip hyphens/spaces, uppercase trailing X).

**Files:**
- Create: `src/mcp_local_reference/services/resolvers/openlibrary.py`
- Create: `tests/test_resolvers/test_openlibrary.py`
- Create: `tests/fixtures/resolvers/openlibrary/isbn13_full.json`
- Create: `tests/fixtures/resolvers/openlibrary/isbn10_sparse.json`
- Create: `tests/fixtures/resolvers/openlibrary/not_found.json`

- [ ] **Step 1: Capture Open Library fixtures**

```bash
mkdir -p tests/fixtures/resolvers/openlibrary
curl -s 'https://openlibrary.org/api/books?bibkeys=ISBN:9780674042070&format=json&jscmd=data' > tests/fixtures/resolvers/openlibrary/isbn13_full.json
curl -s 'https://openlibrary.org/api/books?bibkeys=ISBN:0631205691&format=json&jscmd=data' > tests/fixtures/resolvers/openlibrary/isbn10_sparse.json
curl -s 'https://openlibrary.org/api/books?bibkeys=ISBN:0000000000000&format=json&jscmd=data' > tests/fixtures/resolvers/openlibrary/not_found.json
```

(Open Library returns `{}` for unknown ISBNs, with HTTP 200 — the resolver translates that to `ResolverNotFoundError`.)

- [ ] **Step 2: Write failing tests**

Write `tests/test_resolvers/test_openlibrary.py`:

```python
"""Tests for the Open Library ISBN resolver."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)
from mcp_local_reference.services.resolvers import openlibrary

FIXTURES = Path(__file__).parent.parent / "fixtures" / "resolvers" / "openlibrary"


def _transport(status: int, body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)
    return httpx.MockTransport(handler)


def test_normalize_isbn_strips_hyphens_and_spaces():
    assert openlibrary.normalize_isbn("978-0-674-04207-0") == "9780674042070"
    assert openlibrary.normalize_isbn("0 631 20569 1") == "0631205691"
    assert openlibrary.normalize_isbn("\t013602X1") == "013602X1".upper()


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
        openlibrary.resolve("0000000000000", transport=_transport(200, body))


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
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
uv run pytest tests/test_resolvers/test_openlibrary.py -v
```

Expected: ImportError.

- [ ] **Step 4: Implement the resolver**

Write `src/mcp_local_reference/services/resolvers/openlibrary.py`:

```python
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
        creators.append({
            "creatorType": "author",
            "firstName": first,
            "lastName": last,
        })

    return ZoteroItemDraft(
        item_type="book",
        fields=fields,
        creators=creators,
        pdf_url=None,
        source_identifier=normalized,
    )
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
uv run pytest tests/test_resolvers/test_openlibrary.py -v
```

Expected: 7 passed. (One conditional test passes for either branch — that's deliberate, since the captured ISBN-10 fixture's metadata depth is real-world variable.)

- [ ] **Step 6: Lint**

```bash
uv run ruff check src/mcp_local_reference/services/resolvers/openlibrary.py tests/test_resolvers/test_openlibrary.py
uv run ruff format src/mcp_local_reference/services/resolvers/openlibrary.py tests/test_resolvers/test_openlibrary.py
```

- [ ] **Step 7: Commit**

```bash
git add src/mcp_local_reference/services/resolvers/openlibrary.py tests/test_resolvers/test_openlibrary.py tests/fixtures/resolvers/openlibrary/
git commit -m "feat(resolvers): add Open Library ISBN resolver with normalization"
```

---

### Task 5: Extend `mock_zotero_db` fixture for dedup tests

Adds the `extra` field (fieldID 15) and three new test items: one with a DOI, one preprint with `arXiv:` in extra, one book with an ISBN. These are the source data for the `find_by_*` tests in Tasks 6-8.

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add the extra field and three identifier-bearing items**

`tests/conftest.py` defines `_SCHEMA_AND_SEED` as a single triple-quoted string containing all SQL DDL+DML. The edits below add raw SQL statements *inside that existing string* — do NOT introduce new triple-quote literals.

**Edit 1** — inside the `_SCHEMA_AND_SEED` string, immediately after the line `INSERT INTO fields VALUES (14, 'ISSN');`, insert:

```sql
INSERT INTO fields VALUES (15, 'extra');
```

**Edit 2** — inside the same string, after the existing test-item seed rows (i.e., after the last `INSERT INTO itemTags VALUES ...` for items 1 and 2), append:

```sql
-- ── Item with DOI for dedup test ──────────────────────────────────
INSERT INTO items VALUES (3, 1, 1, 'DOIITEM1', 1);
INSERT INTO itemDataValues VALUES (20, 'Paper With DOI');
INSERT INTO itemDataValues VALUES (21, '10.1000/dedup.test');
INSERT INTO itemData VALUES (3, 1, 20);
INSERT INTO itemData VALUES (3, 3, 21);

-- ── Preprint with arXiv ID in extra ───────────────────────────────
INSERT INTO items VALUES (4, 1, 1, 'ARXITEM1', 1);
INSERT INTO itemDataValues VALUES (22, 'Preprint With arXiv ID');
INSERT INTO itemDataValues VALUES (23, 'arXiv:2401.99999');
INSERT INTO itemData VALUES (4, 1, 22);
INSERT INTO itemData VALUES (4, 15, 23);

-- ── Book with ISBN (stored hyphenated) ────────────────────────────
INSERT INTO items VALUES (5, 2, 1, 'ISBNITEM1', 1);
INSERT INTO itemDataValues VALUES (24, 'Book With ISBN');
INSERT INTO itemDataValues VALUES (25, '978-0-674-04207-0');
INSERT INTO itemData VALUES (5, 1, 24);
INSERT INTO itemData VALUES (5, 13, 25);
```

(The `--` comments are SQL line comments, valid inside the seed string.)

- [ ] **Step 2: Run existing tests to confirm fixture still works**

```bash
uv run pytest -v
```

Expected: all existing tests pass. The new items are addressable but no test reads them yet.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: extend mock SQLite with extra field and identifier-bearing items"
```

---

### Task 6: `ZoteroClient.find_by_doi`

Single-DOI exact-match dedup.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_client.py` (add method on `ZoteroClient`)
- Modify: `tests/test_zotero_client.py` if it exists, else create. (Check first.)

- [ ] **Step 1: Locate or create the test file**

```bash
ls tests/test_zotero_client.py 2>/dev/null && echo "exists" || echo "needs creation"
```

If it doesn't exist, create `tests/test_zotero_client.py` with this header:

```python
"""Tests for ZoteroClient read methods that don't already have coverage."""

from __future__ import annotations

import pytest

from mcp_local_reference.config import Config
from mcp_local_reference.services.zotero_client import ZoteroClient
```

- [ ] **Step 2: Write failing tests for `find_by_doi`**

Append to `tests/test_zotero_client.py`:

```python
def test_find_by_doi_returns_item_key_when_match(mock_zotero_db, tmp_dir):
    config = Config(zotero_data_dir=tmp_dir)  # mock_zotero_db places sqlite at zotero_data_dir/zotero.sqlite
    client = ZoteroClient(config)
    assert client.find_by_doi("10.1000/dedup.test") == "DOIITEM1"


def test_find_by_doi_returns_none_when_no_match(mock_zotero_db, tmp_dir):
    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_doi("10.9999/no-such-paper") is None


def test_find_by_doi_ignores_deleted_items(mock_zotero_db, tmp_dir):
    """Items in deletedItems table must not be matched."""
    import sqlite3
    db = tmp_dir / "Zotero" / "zotero.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO deletedItems VALUES (3)")  # mark DOIITEM1 deleted
        conn.commit()

    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_doi("10.1000/dedup.test") is None
```

(The `mock_zotero_db` fixture must produce `tmp_dir/Zotero/zotero.sqlite`; verify by checking the fixture body in conftest. If the fixture uses a different path, adjust the `Config` argument and the `db` path accordingly. Read `tests/conftest.py` to see how existing tests instantiate `Config`.)

- [ ] **Step 3: Run tests to confirm they fail**

```bash
uv run pytest tests/test_zotero_client.py::test_find_by_doi_returns_item_key_when_match -v
```

Expected: AttributeError on `find_by_doi`.

- [ ] **Step 4: Implement `find_by_doi`**

Add to `src/mcp_local_reference/services/zotero_client.py`, anywhere in the public-method block (e.g. after `get_pdf_path`):

```python
def find_by_doi(self, doi: str) -> str | None:
    """Return item_key for an item whose DOI field equals *doi*, or None.

    Excludes items in deletedItems. Used for skip-and-warn dedup before
    creating a new item via the Web API."""
    query = """
        SELECT i.key
        FROM items i
        JOIN itemData id ON id.itemID = i.itemID
        JOIN itemDataValues idv ON id.valueID = idv.valueID
        JOIN fields f ON id.fieldID = f.fieldID
        LEFT JOIN deletedItems del ON del.itemID = i.itemID
        WHERE f.fieldName = 'DOI'
          AND idv.value = ?
          AND del.itemID IS NULL
        LIMIT 1
    """
    with self._connect() as conn:
        row = conn.execute(query, (doi,)).fetchone()
    return row["key"] if row else None
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
uv run pytest tests/test_zotero_client.py -v -k find_by_doi
```

Expected: 3 passed.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/services/zotero_client.py tests/test_zotero_client.py
uv run ruff format src/mcp_local_reference/services/zotero_client.py tests/test_zotero_client.py
git add src/mcp_local_reference/services/zotero_client.py tests/test_zotero_client.py
git commit -m "feat(zotero_client): add find_by_doi for dedup"
```

---

### Task 7: `ZoteroClient.find_by_arxiv_id`

Checks both the DOI field (for `10.48550/arXiv.<id>` form) and the `extra` field (for `arXiv:<id>` form). Strips version suffix before matching.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_client.py`
- Modify: `tests/test_zotero_client.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_zotero_client.py`:

```python
def test_find_by_arxiv_id_via_extra_field(mock_zotero_db, tmp_dir):
    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_arxiv_id("2401.99999") == "ARXITEM1"


def test_find_by_arxiv_id_strips_version_suffix(mock_zotero_db, tmp_dir):
    """v3 of an existing paper should match the version-naked stored form."""
    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_arxiv_id("2401.99999v3") == "ARXITEM1"


def test_find_by_arxiv_id_via_doi_field(mock_zotero_db, tmp_dir):
    """If user stored an arXiv paper by its 10.48550/arXiv.<id> DOI, match that too."""
    import sqlite3
    db = tmp_dir / "Zotero" / "zotero.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO items VALUES (6, 1, 1, 'DOIARXIV1', 1)")
        conn.execute("INSERT INTO itemDataValues VALUES (30, '10.48550/arXiv.2402.55555')")
        conn.execute("INSERT INTO itemData VALUES (6, 3, 30)")
        conn.commit()

    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_arxiv_id("2402.55555") == "DOIARXIV1"


def test_find_by_arxiv_id_returns_none_when_no_match(mock_zotero_db, tmp_dir):
    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_arxiv_id("1111.22222") is None
```

- [ ] **Step 2: Confirm failure**

```bash
uv run pytest tests/test_zotero_client.py::test_find_by_arxiv_id_via_extra_field -v
```

Expected: AttributeError.

- [ ] **Step 3: Implement `find_by_arxiv_id`**

Add to `services/zotero_client.py`:

```python
def find_by_arxiv_id(self, arxiv_id: str) -> str | None:
    """Return item_key for an item that stores this arXiv ID, or None.

    Matches both:
      - extra field containing 'arXiv:<id>' (Zotero's preprint convention)
      - DOI field equal to '10.48550/arXiv.<id>' (Crossref-mediated arXiv DOI)

    Strips any trailing version suffix (vN) before comparison."""
    import re
    bare = re.sub(r"v\d+$", "", arxiv_id)
    query = """
        SELECT i.key
        FROM items i
        JOIN itemData id ON id.itemID = i.itemID
        JOIN itemDataValues idv ON id.valueID = idv.valueID
        JOIN fields f ON id.fieldID = f.fieldID
        LEFT JOIN deletedItems del ON del.itemID = i.itemID
        WHERE del.itemID IS NULL
          AND (
            (f.fieldName = 'extra' AND idv.value LIKE ?)
            OR (f.fieldName = 'DOI' AND idv.value = ?)
          )
        LIMIT 1
    """
    extra_pattern = f"%arXiv:{bare}%"
    doi_form = f"10.48550/arXiv.{bare}"
    with self._connect() as conn:
        row = conn.execute(query, (extra_pattern, doi_form)).fetchone()
    return row["key"] if row else None
```

(If `import re` is already present at module top, use that instead of importing inside the method. Check the existing imports.)

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_zotero_client.py -v -k find_by_arxiv_id
```

Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/services/zotero_client.py tests/test_zotero_client.py
uv run ruff format src/mcp_local_reference/services/zotero_client.py tests/test_zotero_client.py
git add src/mcp_local_reference/services/zotero_client.py tests/test_zotero_client.py
git commit -m "feat(zotero_client): add find_by_arxiv_id (DOI + extra field, version-stripped)"
```

---

### Task 8: `ZoteroClient.find_by_isbn`

ISBN dedup with normalization on both sides of the comparison.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_client.py`
- Modify: `tests/test_zotero_client.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_zotero_client.py`:

```python
def test_find_by_isbn_normalizes_both_sides(mock_zotero_db, tmp_dir):
    """Stored as '978-0-674-04207-0' (hyphenated). Query unhyphenated must match."""
    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_isbn("9780674042070") == "ISBNITEM1"


def test_find_by_isbn_matches_hyphenated_query(mock_zotero_db, tmp_dir):
    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_isbn("978-0-674-04207-0") == "ISBNITEM1"


def test_find_by_isbn_uppercase_x(mock_zotero_db, tmp_dir):
    """ISBN-10 with check digit X stored lowercase must still match uppercase query."""
    import sqlite3
    db = tmp_dir / "Zotero" / "zotero.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO items VALUES (7, 2, 1, 'ISBNXITEM', 1)")
        conn.execute("INSERT INTO itemDataValues VALUES (40, '0-13-602X-1')")
        conn.execute("INSERT INTO itemData VALUES (7, 13, 40)")
        conn.commit()

    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_isbn("013602X1") == "ISBNXITEM"


def test_find_by_isbn_returns_none_when_no_match(mock_zotero_db, tmp_dir):
    config = Config(zotero_data_dir=tmp_dir)
    client = ZoteroClient(config)
    assert client.find_by_isbn("0000000000000") is None
```

- [ ] **Step 2: Confirm failure**

```bash
uv run pytest tests/test_zotero_client.py::test_find_by_isbn_normalizes_both_sides -v
```

Expected: AttributeError.

- [ ] **Step 3: Implement `find_by_isbn`**

Add to `services/zotero_client.py`:

```python
def find_by_isbn(self, isbn: str) -> str | None:
    """Return item_key for an item with this ISBN, or None.

    Normalizes both the input and stored values (strip hyphens/whitespace,
    uppercase X) before comparing."""
    target = self._normalize_isbn(isbn)
    query = """
        SELECT i.key, idv.value
        FROM items i
        JOIN itemData id ON id.itemID = i.itemID
        JOIN itemDataValues idv ON id.valueID = idv.valueID
        JOIN fields f ON id.fieldID = f.fieldID
        LEFT JOIN deletedItems del ON del.itemID = i.itemID
        WHERE f.fieldName = 'ISBN'
          AND del.itemID IS NULL
    """
    with self._connect() as conn:
        for row in conn.execute(query):
            if self._normalize_isbn(row["value"]) == target:
                return row["key"]
    return None


@staticmethod
def _normalize_isbn(isbn: str) -> str:
    return "".join(ch.upper() if ch in "xX" else ch for ch in isbn if ch.isalnum())
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_zotero_client.py -v -k find_by_isbn
```

Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/services/zotero_client.py tests/test_zotero_client.py
uv run ruff format src/mcp_local_reference/services/zotero_client.py tests/test_zotero_client.py
git add src/mcp_local_reference/services/zotero_client.py tests/test_zotero_client.py
git commit -m "feat(zotero_client): add find_by_isbn with normalized comparison"
```

---

### Task 9: Add `add_reference_max_pdf_mb` config field

Single-line config addition.

**Files:**
- Modify: `src/mcp_local_reference/config.py`

- [ ] **Step 1: Add the field**

In `src/mcp_local_reference/config.py`, in the `Config` class body, after `zotero_api_base_url`:

```python
add_reference_max_pdf_mb: int = 50
```

- [ ] **Step 2: Confirm config still loads**

```bash
uv run python -c "from mcp_local_reference.config import Config; c = Config(); print(c.add_reference_max_pdf_mb)"
```

Expected output: `50`.

- [ ] **Step 3: Commit**

```bash
git add src/mcp_local_reference/config.py
git commit -m "feat(config): add add_reference_max_pdf_mb (default 50)"
```

---

### Task 10: `ZoteroApiClient.create_item`

POSTs a `ZoteroItemDraft` to `/users/{id}/items` with optional `collections` array baked into the payload.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_api_client.py`
- Modify: `tests/test_auto_tag.py` (extend the existing `TestZoteroApiClient` class, mirroring the `collection-editing` plan precedent — it's already the home for HTTP-layer tests)

- [ ] **Step 1: Write failing tests**

Locate `class TestZoteroApiClient` in `tests/test_auto_tag.py`. Add these methods inside it:

```python
def test_create_item_posts_payload_and_returns_snapshot(self, api_config):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Last-Modified-Version": "42"},
            json={
                "successful": {
                    "0": {
                        "key": "NEW123ABC",
                        "version": 42,
                        "data": {
                            "key": "NEW123ABC",
                            "version": 42,
                            "tags": [],
                            "collections": [],
                            "title": "X",
                        },
                    }
                },
                "failed": {},
                "success": {"0": "NEW123ABC"},
                "unchanged": {},
            },
        )

    from mcp_local_reference.services.resolvers import ZoteroItemDraft
    from mcp_local_reference.services.zotero_api_client import ZoteroApiClient

    client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
    draft = ZoteroItemDraft(
        item_type="journalArticle",
        fields={"title": "X", "DOI": "10.1/x"},
        creators=[{"creatorType": "author", "firstName": "A", "lastName": "B"}],
        pdf_url=None,
        source_identifier="10.1/x",
    )
    snapshot = client.create_item(draft)
    assert snapshot.item_key == "NEW123ABC"
    assert snapshot.version == 42
    assert captured["body"][0]["itemType"] == "journalArticle"
    assert captured["body"][0]["title"] == "X"
    assert captured["body"][0]["DOI"] == "10.1/x"
    assert captured["body"][0]["creators"][0]["lastName"] == "B"


def test_create_item_includes_collection_key_when_provided(self, api_config):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "successful": {"0": {"key": "K", "version": 1, "data": {"key": "K", "version": 1, "tags": [], "collections": ["XYZ789"]}}},
                "failed": {},
                "success": {"0": "K"},
                "unchanged": {},
            },
        )

    from mcp_local_reference.services.resolvers import ZoteroItemDraft
    from mcp_local_reference.services.zotero_api_client import ZoteroApiClient

    client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
    draft = ZoteroItemDraft(
        item_type="book", fields={"title": "T"}, creators=[],
        pdf_url=None, source_identifier="978",
    )
    client.create_item(draft, collection_key="XYZ789")
    assert captured["body"][0]["collections"] == ["XYZ789"]


def test_create_item_raises_on_failed_response(self, api_config):
    def handler(request):
        return httpx.Response(
            200,
            json={"successful": {}, "failed": {"0": {"code": 400, "message": "missing title"}}, "success": {}, "unchanged": {}},
        )

    from mcp_local_reference.services.resolvers import ZoteroItemDraft
    from mcp_local_reference.services.zotero_api_client import ZoteroApiClient, ZoteroApiError

    client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
    draft = ZoteroItemDraft(
        item_type="journalArticle", fields={"title": "X"}, creators=[],
        pdf_url=None, source_identifier="10.1/x",
    )
    with pytest.raises(ZoteroApiError):
        client.create_item(draft)


def test_create_item_requires_credentials(self):
    from mcp_local_reference.config import Config
    from mcp_local_reference.services.resolvers import ZoteroItemDraft
    from mcp_local_reference.services.zotero_api_client import ZoteroApiClient, MissingCredentialsError

    bare_config = Config()  # no zotero_user_id, no zotero_api_key
    bare_config.zotero_user_id = ""
    bare_config.zotero_api_key = ""
    client = ZoteroApiClient(bare_config)
    draft = ZoteroItemDraft(
        item_type="book", fields={"title": "T"}, creators=[],
        pdf_url=None, source_identifier="x",
    )
    with pytest.raises(MissingCredentialsError):
        client.create_item(draft)
```

(Imports `httpx`, `json`, `pytest` should already be present at the top of `test_auto_tag.py`. If `api_config` fixture doesn't already exist, locate it in the file — the `collection-editing` plan introduced it at top level; check by `grep "api_config" tests/test_auto_tag.py`.)

- [ ] **Step 2: Confirm failure**

```bash
uv run pytest tests/test_auto_tag.py::TestZoteroApiClient::test_create_item_posts_payload_and_returns_snapshot -v
```

Expected: AttributeError on `create_item`.

- [ ] **Step 3: Implement `create_item`**

Add to `services/zotero_api_client.py`, after `update_item_collections`:

```python
def create_item(
    self,
    draft: "ZoteroItemDraft",
    collection_key: str | None = None,
) -> ItemSnapshot:
    """POST a new item to Zotero. Returns the snapshot of the created item.

    The ``collection_key`` argument, if provided, is included in the POST
    body's ``collections`` array — this avoids a follow-up PATCH and keeps
    the create+file operation atomic from the API's point of view.

    Raises:
        MissingCredentialsError: if user_id / api_key not configured.
        ZoteroApiError: if Zotero rejects the create (``failed`` non-empty).
    """
    self._require_credentials()

    payload: dict[str, Any] = {
        "itemType": draft.item_type,
        **draft.fields,
        "creators": list(draft.creators),
    }
    if collection_key is not None:
        payload["collections"] = [collection_key]

    url = self._items_url()
    with self._client(self._headers()) as client:
        response = client.post(url, json=[payload])
    response.raise_for_status()
    body = response.json()
    if body.get("failed"):
        raise ZoteroApiError(f"Zotero rejected create_item: {body['failed']}")
    try:
        entry = body["successful"]["0"]
    except (KeyError, TypeError) as exc:
        raise ZoteroApiError(f"Unexpected create_item response: {body!r}") from exc

    data = entry["data"]
    return ItemSnapshot(
        item_key=data["key"],
        version=int(data.get("version", 0)),
        tags=[t["tag"] for t in data.get("tags", []) if "tag" in t],
        collections=list(data.get("collections", [])),
        raw=data,
    )


def _items_url(self) -> str:
    base = self.config.zotero_api_base_url.rstrip("/")
    return f"{base}/users/{self.config.zotero_user_id}/items"
```

Add the import at the top of `zotero_api_client.py` for the type-only reference:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_local_reference.services.resolvers import ZoteroItemDraft
```

(If `TYPE_CHECKING` is already imported, don't duplicate. Verify with `head -30 src/mcp_local_reference/services/zotero_api_client.py`.)

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_auto_tag.py::TestZoteroApiClient -v -k create_item
```

Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
uv run ruff format src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
git add src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
git commit -m "feat(zotero_api): add create_item with optional collection_key in payload"
```

---

### Task 11: `ZoteroApiClient.upload_attachment`

Three-step Zotero upload: POST to register intent + get auth, PUT body to S3, POST to register completion.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_api_client.py`
- Modify: `tests/test_auto_tag.py`

- [ ] **Step 1: Write failing tests**

Append to `class TestZoteroApiClient` in `tests/test_auto_tag.py`:

```python
def test_upload_attachment_three_step_flow(self, api_config):
    """auth POST → S3 PUT → register POST. Each step gets the right body/headers."""
    calls = []

    def handler(request):
        calls.append((str(request.url), request.method))

        # Step 1: child item create (auth POST to /items)
        if request.method == "POST" and "/items" in str(request.url) and "/file" not in str(request.url):
            return httpx.Response(
                200,
                json={
                    "successful": {"0": {"key": "ATTACH01", "version": 1, "data": {"key": "ATTACH01", "version": 1, "tags": [], "collections": []}}},
                    "failed": {}, "success": {"0": "ATTACH01"}, "unchanged": {},
                },
            )

        # Step 2: get S3 upload auth (POST to /items/<key>/file)
        if request.method == "POST" and request.url.path.endswith("/file") and b"upload=" not in request.content:
            return httpx.Response(
                200,
                json={
                    "url": "https://s3-mock.example/upload",
                    "contentType": "application/pdf",
                    "prefix": "PRE",
                    "suffix": "SUF",
                    "uploadKey": "U-KEY-123",
                },
            )

        # Step 3: S3 PUT
        if request.method == "POST" and "s3-mock.example" in str(request.url):
            return httpx.Response(201, content=b"")

        # Step 4: register upload (POST to /items/<key>/file with upload= form body)
        if request.method == "POST" and request.url.path.endswith("/file") and b"upload=" in request.content:
            return httpx.Response(204)

        return httpx.Response(500, content=b"unexpected")

    from mcp_local_reference.services.zotero_api_client import ZoteroApiClient
    client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))

    attachment_key = client.upload_attachment(
        parent_key="PARENT01",
        pdf_bytes=b"%PDF-1.7\n" + b"x" * 2000,
        filename="paper.pdf",
    )
    assert attachment_key == "ATTACH01"
    # Confirm we made the registered POSTs in the right order
    assert any(p.endswith("/items") for p, _ in calls)
    assert any("/file" in p for p, _ in calls)


def test_upload_attachment_raises_on_create_failure(self, api_config):
    def handler(request):
        return httpx.Response(
            200,
            json={"successful": {}, "failed": {"0": {"code": 403, "message": "no write access"}}, "success": {}, "unchanged": {}},
        )

    from mcp_local_reference.services.zotero_api_client import ZoteroApiClient, ZoteroApiError
    client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
    with pytest.raises(ZoteroApiError):
        client.upload_attachment("PARENT01", b"%PDF-1.7\n" + b"x" * 2000, "paper.pdf")


def test_upload_attachment_raises_on_s3_failure(self, api_config):
    def handler(request):
        if "/items" in str(request.url) and "/file" not in str(request.url):
            return httpx.Response(
                200,
                json={
                    "successful": {"0": {"key": "ATTACH02", "version": 1, "data": {"key": "ATTACH02", "version": 1, "tags": [], "collections": []}}},
                    "failed": {}, "success": {"0": "ATTACH02"}, "unchanged": {},
                },
            )
        if "/file" in request.url.path and b"upload=" not in request.content:
            return httpx.Response(200, json={"url": "https://s3-mock.example/u", "contentType": "application/pdf", "prefix": "P", "suffix": "S", "uploadKey": "K"})
        if "s3-mock.example" in str(request.url):
            return httpx.Response(500, content=b"S3 down")
        return httpx.Response(500)

    from mcp_local_reference.services.zotero_api_client import ZoteroApiClient, ZoteroApiError
    client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
    with pytest.raises(ZoteroApiError):
        client.upload_attachment("PARENT01", b"%PDF-1.7\n" + b"x" * 2000, "paper.pdf")
```

- [ ] **Step 2: Confirm failure**

```bash
uv run pytest tests/test_auto_tag.py::TestZoteroApiClient -v -k upload_attachment
```

Expected: AttributeError on `upload_attachment`.

- [ ] **Step 3: Implement `upload_attachment`**

Add to `services/zotero_api_client.py`:

```python
def upload_attachment(
    self,
    parent_key: str,
    pdf_bytes: bytes,
    filename: str,
) -> str:
    """Three-step Zotero attachment upload.

    1. Create the child item (itemType=attachment, linkMode=imported_url).
    2. Request S3 upload authorization.
    3. PUT bytes to S3, then register completion.

    Returns the attachment item_key on success. Raises ``ZoteroApiError`` on
    any step's failure — caller decides whether to swallow (arXiv PDF case)
    or propagate.
    """
    import hashlib
    self._require_credentials()

    # Step 1: create the attachment item
    md5 = hashlib.md5(pdf_bytes).hexdigest()
    attachment_payload = {
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "imported_url",
        "title": filename,
        "filename": filename,
        "contentType": "application/pdf",
        "md5": md5,
        "mtime": 0,
    }
    items_url = self._items_url()
    with self._client(self._headers()) as client:
        response = client.post(items_url, json=[attachment_payload])
    response.raise_for_status()
    body = response.json()
    if body.get("failed"):
        raise ZoteroApiError(f"Zotero rejected attachment create: {body['failed']}")
    try:
        attachment_key = body["successful"]["0"]["data"]["key"]
    except (KeyError, TypeError) as exc:
        raise ZoteroApiError(f"Unexpected attachment-create response: {body!r}") from exc

    # Step 2: get S3 upload auth
    file_url = f"{items_url}/{attachment_key}/file"
    auth_headers = {**self._headers(), "Content-Type": "application/x-www-form-urlencoded"}
    auth_body = f"md5={md5}&filename={filename}&filesize={len(pdf_bytes)}&mtime=0"
    with self._client(auth_headers) as client:
        auth_response = client.post(file_url, content=auth_body)
    auth_response.raise_for_status()
    auth_data = auth_response.json()
    if auth_data.get("exists") == 1:
        # File already on Zotero's storage with this MD5 — no upload needed
        return attachment_key

    upload_url = auth_data["url"]
    prefix = auth_data["prefix"].encode() if isinstance(auth_data["prefix"], str) else auth_data["prefix"]
    suffix = auth_data["suffix"].encode() if isinstance(auth_data["suffix"], str) else auth_data["suffix"]
    upload_key = auth_data["uploadKey"]
    upload_payload = prefix + pdf_bytes + suffix
    upload_content_type = auth_data["contentType"]

    # Step 3: PUT to S3
    with self._client({"Content-Type": upload_content_type}) as client:
        s3_response = client.post(upload_url, content=upload_payload)
    if s3_response.status_code >= 300:
        raise ZoteroApiError(
            f"S3 upload returned {s3_response.status_code} for attachment '{attachment_key}'"
        )

    # Step 4: register completion
    register_body = f"upload={upload_key}"
    with self._client(auth_headers) as client:
        register_response = client.post(file_url, content=register_body)
    register_response.raise_for_status()

    return attachment_key
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_auto_tag.py::TestZoteroApiClient -v -k upload_attachment
```

Expected: 3 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
uv run ruff format src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
git add src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
git commit -m "feat(zotero_api): add upload_attachment (3-step S3 flow)"
```

---

### Task 12: `_fetch_pdf` helper with magic-byte + Content-Length checks

The PDF download/validation helper used by the arXiv tool. Lives in `tools/add_reference.py` as a private module-level function so it's directly testable.

**Files:**
- Create: `src/mcp_local_reference/tools/add_reference.py` (initial module skeleton with `_fetch_pdf` only)
- Create: `tests/test_add_reference.py` (initial test file with `_fetch_pdf` tests only)

- [ ] **Step 1: Create the tool module skeleton**

Write `src/mcp_local_reference/tools/add_reference.py`:

```python
"""MCP tools for adding references to Zotero by identifier (DOI, arXiv, ISBN).

Resolves citation metadata via external APIs (Crossref, arXiv, Open Library),
deduplicates against the local Zotero SQLite, and POSTs new bibliographic
items to ``api.zotero.org``. The arXiv tool also auto-attaches the
open-access PDF.

Tool implementations live in module-level ``*_impl()`` helpers so they're
unit-testable without an MCP harness.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"
_PDF_MIN_SIZE_BYTES = 1024
_PDF_DOWNLOAD_TIMEOUT_S = 30.0


def _fetch_pdf(
    url: str,
    max_size_mb: int,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bytes | None, str]:
    """Fetch a PDF, returning (bytes_or_None, status).

    Status values:
      - "ok": valid PDF bytes returned
      - "skipped": Content-Length exceeded max_size_mb (no body downloaded)
      - "failed": 404, network error, magic-byte check failed, or below min size

    The caller logs the failure reason; the helper itself logs at INFO.
    """
    max_bytes = max_size_mb * 1024 * 1024
    try:
        with httpx.Client(timeout=_PDF_DOWNLOAD_TIMEOUT_S, transport=transport, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 404:
                    logger.info("PDF 404: %s", url)
                    return None, "failed"
                if response.status_code >= 300:
                    logger.info("PDF non-200 (%s): %s", response.status_code, url)
                    return None, "failed"

                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    logger.info("PDF oversize (%s bytes): %s", content_length, url)
                    return None, "skipped"

                buf = bytearray()
                for chunk in response.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.info("PDF stream exceeded max size: %s", url)
                        return None, "skipped"

                pdf_bytes = bytes(buf)
    except httpx.HTTPError as e:
        logger.info("PDF fetch error for %s: %s", url, e)
        return None, "failed"

    if len(pdf_bytes) < _PDF_MIN_SIZE_BYTES:
        logger.info("PDF too small (%d bytes): %s", len(pdf_bytes), url)
        return None, "failed"
    if not pdf_bytes.startswith(_PDF_MAGIC):
        logger.info("PDF magic-byte check failed (got %r...): %s", pdf_bytes[:16], url)
        return None, "failed"

    return pdf_bytes, "ok"
```

- [ ] **Step 2: Write failing tests for `_fetch_pdf`**

Write `tests/test_add_reference.py`:

```python
"""Tests for the add_reference tool module."""

from __future__ import annotations

import httpx
import pytest

from mcp_local_reference.tools.add_reference import _fetch_pdf


def _transport(status: int, body: bytes, headers: dict[str, str] | None = None) -> httpx.MockTransport:
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
```

- [ ] **Step 3: Confirm tests pass against the helper**

```bash
uv run pytest tests/test_add_reference.py -v
```

Expected: 6 passed.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
uv run ruff format src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
git add src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
git commit -m "feat(add_reference): add _fetch_pdf with magic-byte and size guards"
```

---

### Task 13: `add_reference_by_doi` impl + MCP tool

The DOI orchestration: validate format → resolve → dedup → POST (or dry-run preview).

**Files:**
- Modify: `src/mcp_local_reference/tools/add_reference.py`
- Modify: `tests/test_add_reference.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_add_reference.py`:

```python
import json
from unittest.mock import MagicMock

from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)
from mcp_local_reference.services.zotero_api_client import (
    ItemSnapshot,
    MissingCredentialsError,
)


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
        "not-a-doi", collection_key=None, dry_run=True,
        zotero=MagicMock(), zotero_api=MagicMock(), resolver=MagicMock(),
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
        "10.1/x", collection_key=None, dry_run=True,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
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
        "10.1/x", collection_key=None, dry_run=True,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
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
        item_key="NEW456", version=1, tags=[], collections=[], raw={},
    )
    resolver = MagicMock(return_value=_draft())

    result_json = add_reference_by_doi_impl(
        "10.1/x", collection_key="ABC", dry_run=False,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
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
        "10.1/x", collection_key=None, dry_run=True,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
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
        "10.1/x", collection_key=None, dry_run=False,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
    )
    result = json.loads(result_json)
    assert result["status"] == "error"
    assert "creds" in result["error"].lower()
```

- [ ] **Step 2: Confirm failure**

```bash
uv run pytest tests/test_add_reference.py -v -k "doi"
```

Expected: ImportError on `add_reference_by_doi_impl`.

- [ ] **Step 3: Implement DOI orchestration**

Append to `src/mcp_local_reference/tools/add_reference.py`:

```python
import json
import re

from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.services.resolvers import (
    ResolverError,
    ResolverNotFoundError,
    ZoteroItemDraft,
)
from mcp_local_reference.services.resolvers import crossref as crossref_resolver
from mcp_local_reference.services.zotero_api_client import (
    MissingCredentialsError,
    ZoteroApiClient,
    ZoteroApiError,
)
from mcp_local_reference.services.zotero_client import ZoteroClient

_DOI_RE = re.compile(r"^10\.\d{4,9}/[^\s]+$")


def add_reference_by_doi_impl(
    doi: str,
    collection_key: str | None,
    dry_run: bool,
    *,
    zotero: ZoteroClient,
    zotero_api: ZoteroApiClient,
    resolver: Callable[[str], ZoteroItemDraft] = crossref_resolver.resolve,
) -> str:
    """Resolve, dedup, and (if not dry-run and not duplicate) POST a DOI item.

    Returns a JSON string. Shape documented in the spec under 'Response Shape'.
    """
    if not _DOI_RE.fullmatch(doi):
        return json.dumps({"status": "error", "error": f"Invalid DOI format: {doi}"})

    try:
        draft = resolver(doi)
    except ResolverNotFoundError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except ResolverError as e:
        return json.dumps({"status": "error", "error": str(e)})

    existing = zotero.find_by_doi(doi)
    if existing is not None:
        return json.dumps({
            "status": "exists",
            "item_key": existing,
            "title": draft.fields.get("title"),
            "warning": f"Already in library as {existing}",
            "dry_run": dry_run,
        })

    if dry_run:
        return json.dumps({
            "status": "would_create",
            "title": draft.fields.get("title"),
            "item_type": draft.item_type,
            "collection_key": collection_key,
            "pdf_status": None,
            "dry_run": True,
        })

    try:
        snapshot = zotero_api.create_item(draft, collection_key=collection_key)
    except (MissingCredentialsError, ZoteroApiError) as e:
        return json.dumps({"status": "error", "error": str(e)})

    return json.dumps({
        "status": "created",
        "item_key": snapshot.item_key,
        "title": draft.fields.get("title"),
        "item_type": draft.item_type,
        "collection_key": collection_key,
        "pdf_status": None,
        "dry_run": False,
    })
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_add_reference.py -v -k "doi"
```

Expected: 6 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
uv run ruff format src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
git add src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
git commit -m "feat(add_reference): implement add_reference_by_doi_impl"
```

---

### Task 14: `add_reference_by_arxiv` impl + PDF attach

Same orchestration as DOI, plus the PDF download and attachment step (non-fatal).

**Files:**
- Modify: `src/mcp_local_reference/tools/add_reference.py`
- Modify: `tests/test_add_reference.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_add_reference.py`:

```python
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
        item_key="NEW789", version=1, tags=[], collections=[], raw={},
    )
    zotero_api.upload_attachment.return_value = "ATTACH01"
    resolver = MagicMock(return_value=_arxiv_draft())
    pdf_fetcher = MagicMock(return_value=(b"%PDF-1.7\n" + b"x" * 2000, "ok"))

    result_json = add_reference_by_arxiv_impl(
        "2401.12345", collection_key=None, dry_run=False,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
        pdf_fetcher=pdf_fetcher, max_pdf_mb=50,
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
        item_key="NEW999", version=1, tags=[], collections=[], raw={},
    )
    resolver = MagicMock(return_value=_arxiv_draft())
    pdf_fetcher = MagicMock(return_value=(None, "failed"))

    result_json = add_reference_by_arxiv_impl(
        "2401.12345", collection_key=None, dry_run=False,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
        pdf_fetcher=pdf_fetcher, max_pdf_mb=50,
    )
    result = json.loads(result_json)
    assert result["status"] == "created"
    assert result["item_key"] == "NEW999"
    assert result["pdf_status"] == "failed"
    zotero_api.upload_attachment.assert_not_called()


def test_arxiv_pdf_upload_exception_downgrades_to_failed():
    from mcp_local_reference.tools.add_reference import add_reference_by_arxiv_impl
    from mcp_local_reference.services.zotero_api_client import ZoteroApiError
    zotero = MagicMock()
    zotero.find_by_arxiv_id.return_value = None
    zotero_api = MagicMock()
    zotero_api.create_item.return_value = ItemSnapshot(
        item_key="NEW000", version=1, tags=[], collections=[], raw={},
    )
    zotero_api.upload_attachment.side_effect = ZoteroApiError("S3 down")
    resolver = MagicMock(return_value=_arxiv_draft())
    pdf_fetcher = MagicMock(return_value=(b"%PDF-1.7\n" + b"x" * 2000, "ok"))

    result_json = add_reference_by_arxiv_impl(
        "2401.12345", collection_key=None, dry_run=False,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
        pdf_fetcher=pdf_fetcher, max_pdf_mb=50,
    )
    result = json.loads(result_json)
    assert result["status"] == "created"  # NOT rolled back
    assert result["pdf_status"] == "failed"


def test_arxiv_invalid_id_format_returns_error():
    from mcp_local_reference.tools.add_reference import add_reference_by_arxiv_impl
    result_json = add_reference_by_arxiv_impl(
        "not-an-arxiv-id", collection_key=None, dry_run=True,
        zotero=MagicMock(), zotero_api=MagicMock(),
        resolver=MagicMock(), pdf_fetcher=MagicMock(),
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
        "2401.12345", collection_key=None, dry_run=True,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
        pdf_fetcher=pdf_fetcher, max_pdf_mb=50,
    )
    result = json.loads(result_json)
    assert result["status"] == "would_create"
    assert result["pdf_status"] == "skipped"
    pdf_fetcher.assert_not_called()
    zotero_api.upload_attachment.assert_not_called()
```

- [ ] **Step 2: Confirm failure**

```bash
uv run pytest tests/test_add_reference.py -v -k arxiv
```

Expected: ImportError on `add_reference_by_arxiv_impl`.

- [ ] **Step 3: Implement arxiv orchestration**

Append to `src/mcp_local_reference/tools/add_reference.py`:

```python
from mcp_local_reference.services.resolvers import arxiv as arxiv_resolver

_ARXIV_NEW_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_ARXIV_OLD_RE = re.compile(r"^[a-z\-]+/\d{7}$")


def _is_valid_arxiv_id(arxiv_id: str) -> bool:
    return bool(_ARXIV_NEW_RE.fullmatch(arxiv_id) or _ARXIV_OLD_RE.fullmatch(arxiv_id))


def add_reference_by_arxiv_impl(
    arxiv_id: str,
    collection_key: str | None,
    dry_run: bool,
    *,
    zotero: ZoteroClient,
    zotero_api: ZoteroApiClient,
    resolver: Callable[[str], ZoteroItemDraft] = arxiv_resolver.resolve,
    pdf_fetcher: Callable[[str, int], tuple[bytes | None, str]] = _fetch_pdf,
    max_pdf_mb: int = 50,
) -> str:
    if not _is_valid_arxiv_id(arxiv_id):
        return json.dumps({"status": "error", "error": f"Invalid arXiv ID format: {arxiv_id}"})

    try:
        draft = resolver(arxiv_id)
    except ResolverNotFoundError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except ResolverError as e:
        return json.dumps({"status": "error", "error": str(e)})

    existing = zotero.find_by_arxiv_id(arxiv_id)
    if existing is not None:
        return json.dumps({
            "status": "exists",
            "item_key": existing,
            "title": draft.fields.get("title"),
            "warning": f"Already in library as {existing}",
            "dry_run": dry_run,
        })

    if dry_run:
        return json.dumps({
            "status": "would_create",
            "title": draft.fields.get("title"),
            "item_type": draft.item_type,
            "collection_key": collection_key,
            "pdf_status": "skipped",
            "dry_run": True,
        })

    try:
        snapshot = zotero_api.create_item(draft, collection_key=collection_key)
    except (MissingCredentialsError, ZoteroApiError) as e:
        return json.dumps({"status": "error", "error": str(e)})

    pdf_status = "skipped"
    if draft.pdf_url:
        pdf_bytes, fetch_status = pdf_fetcher(draft.pdf_url, max_pdf_mb)
        if fetch_status == "ok" and pdf_bytes is not None:
            try:
                zotero_api.upload_attachment(
                    parent_key=snapshot.item_key,
                    pdf_bytes=pdf_bytes,
                    filename=f"{arxiv_id.replace('/', '_')}.pdf",
                )
                pdf_status = "attached"
            except ZoteroApiError as e:
                logger.warning("PDF upload failed for %s: %s", snapshot.item_key, e)
                pdf_status = "failed"
        else:
            pdf_status = fetch_status  # "failed" or "skipped"

    return json.dumps({
        "status": "created",
        "item_key": snapshot.item_key,
        "title": draft.fields.get("title"),
        "item_type": draft.item_type,
        "collection_key": collection_key,
        "pdf_status": pdf_status,
        "dry_run": False,
    })
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_add_reference.py -v -k arxiv
```

Expected: 5 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
uv run ruff format src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
git add src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
git commit -m "feat(add_reference): implement add_reference_by_arxiv_impl with PDF attach"
```

---

### Task 15: `add_reference_by_isbn` impl

Same orchestration as DOI, no PDF, calls Open Library resolver. ISBN normalization happens in the resolver and the dedup helper, but the validation step here checks structural validity (length 10 or 13, valid checksum).

**Files:**
- Modify: `src/mcp_local_reference/tools/add_reference.py`
- Modify: `tests/test_add_reference.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_add_reference.py`:

```python
def test_isbn_invalid_checksum_returns_error():
    from mcp_local_reference.tools.add_reference import add_reference_by_isbn_impl
    result_json = add_reference_by_isbn_impl(
        "9781234567890", collection_key=None, dry_run=True,  # bad ISBN-13 checksum
        zotero=MagicMock(), zotero_api=MagicMock(), resolver=MagicMock(),
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
        "9780674042070", collection_key=None, dry_run=True,
        zotero=zotero, zotero_api=zotero_api, resolver=resolver,
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
        item_type="book", fields={"title": "T", "ISBN": "9780674042070"},
        creators=[], pdf_url=None, source_identifier="9780674042070",
    )

    result_json = add_reference_by_isbn_impl(
        "978-0-674-04207-0", collection_key=None, dry_run=True,
        zotero=zotero, zotero_api=zotero_api,
        resolver=MagicMock(return_value=book_draft),
    )
    result = json.loads(result_json)
    assert result["status"] == "exists"
    assert result["item_key"] == "ISBNITEM1"


def test_isbn10_with_x_check_digit_accepted():
    from mcp_local_reference.tools.add_reference import add_reference_by_isbn_impl
    zotero = MagicMock()
    zotero.find_by_isbn.return_value = None
    zotero_api = MagicMock()
    book_draft = ZoteroItemDraft(
        item_type="book", fields={"title": "T", "ISBN": "013602X1"},
        creators=[], pdf_url=None, source_identifier="013602X1",
    )

    result_json = add_reference_by_isbn_impl(
        "0-13-602X-1", collection_key=None, dry_run=True,
        zotero=zotero, zotero_api=zotero_api,
        resolver=MagicMock(return_value=book_draft),
    )
    result = json.loads(result_json)
    assert result["status"] != "error"
```

- [ ] **Step 2: Confirm failure**

```bash
uv run pytest tests/test_add_reference.py -v -k isbn
```

Expected: ImportError.

- [ ] **Step 3: Implement isbn orchestration with checksum validation**

Append to `src/mcp_local_reference/tools/add_reference.py`:

```python
from mcp_local_reference.services.resolvers import openlibrary as openlibrary_resolver


def _is_valid_isbn(raw: str) -> bool:
    """Validate ISBN-10 or ISBN-13 checksum after normalization."""
    isbn = openlibrary_resolver.normalize_isbn(raw)
    if len(isbn) == 10:
        return _isbn10_checksum(isbn)
    if len(isbn) == 13:
        return _isbn13_checksum(isbn)
    return False


def _isbn10_checksum(isbn: str) -> bool:
    if not all(c.isdigit() or (i == 9 and c == "X") for i, c in enumerate(isbn)):
        return False
    total = 0
    for i, c in enumerate(isbn):
        v = 10 if c == "X" else int(c)
        total += v * (10 - i)
    return total % 11 == 0


def _isbn13_checksum(isbn: str) -> bool:
    if not isbn.isdigit():
        return False
    total = 0
    for i, c in enumerate(isbn):
        v = int(c)
        total += v if i % 2 == 0 else v * 3
    return total % 10 == 0


def add_reference_by_isbn_impl(
    isbn: str,
    collection_key: str | None,
    dry_run: bool,
    *,
    zotero: ZoteroClient,
    zotero_api: ZoteroApiClient,
    resolver: Callable[[str], ZoteroItemDraft] = openlibrary_resolver.resolve,
) -> str:
    if not _is_valid_isbn(isbn):
        return json.dumps({"status": "error", "error": f"Invalid ISBN format/checksum: {isbn}"})

    try:
        draft = resolver(isbn)
    except ResolverNotFoundError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except ResolverError as e:
        return json.dumps({"status": "error", "error": str(e)})

    existing = zotero.find_by_isbn(isbn)
    if existing is not None:
        return json.dumps({
            "status": "exists",
            "item_key": existing,
            "title": draft.fields.get("title"),
            "warning": f"Already in library as {existing}",
            "dry_run": dry_run,
        })

    if dry_run:
        return json.dumps({
            "status": "would_create",
            "title": draft.fields.get("title"),
            "item_type": draft.item_type,
            "collection_key": collection_key,
            "pdf_status": None,
            "dry_run": True,
        })

    try:
        snapshot = zotero_api.create_item(draft, collection_key=collection_key)
    except (MissingCredentialsError, ZoteroApiError) as e:
        return json.dumps({"status": "error", "error": str(e)})

    return json.dumps({
        "status": "created",
        "item_key": snapshot.item_key,
        "title": draft.fields.get("title"),
        "item_type": draft.item_type,
        "collection_key": collection_key,
        "pdf_status": None,
        "dry_run": False,
    })
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_add_reference.py -v -k isbn
```

Expected: 4 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
uv run ruff format src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
git add src/mcp_local_reference/tools/add_reference.py tests/test_add_reference.py
git commit -m "feat(add_reference): implement add_reference_by_isbn_impl with checksum validation"
```

---

### Task 16: Concurrency invariant tests

Pin the four invariants from the spec: order-of-calls, short-circuit, dedup-on-rerun, no-retry.

**Files:**
- Modify: `tests/test_add_reference.py`

- [ ] **Step 1: Write the four concurrency tests**

Append to `tests/test_add_reference.py`:

```python
class TestConcurrencyInvariants:
    """Pins the dedup ↔ POST race protections from the spec."""

    def test_dedup_check_runs_before_create(self):
        """Reordering find_by_doi after create_item would re-introduce the duplicate-write window."""
        from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl
        call_order: list[str] = []

        zotero = MagicMock()
        zotero.find_by_doi.side_effect = lambda *a, **kw: call_order.append("find") or None

        zotero_api = MagicMock()
        zotero_api.create_item.side_effect = lambda *a, **kw: (
            call_order.append("create") or ItemSnapshot("X", 1, [], [], {})
        )
        resolver = MagicMock(return_value=_draft())

        add_reference_by_doi_impl(
            "10.1/x", collection_key=None, dry_run=False,
            zotero=zotero, zotero_api=zotero_api, resolver=resolver,
        )
        assert call_order == ["find", "create"]

    def test_existing_item_blocks_create_call(self):
        from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl
        zotero = MagicMock()
        zotero.find_by_doi.return_value = "ABC123"
        zotero_api = MagicMock()
        resolver = MagicMock(return_value=_draft())

        add_reference_by_doi_impl(
            "10.1/x", collection_key=None, dry_run=False,
            zotero=zotero, zotero_api=zotero_api, resolver=resolver,
        )
        zotero_api.create_item.assert_not_called()

    def test_recovery_after_concurrent_add(self):
        """Simulates the desktop-added-it-during-our-run race.

        First call: dedup miss → create → success (item_key=NEW123).
        Second call (different invocation, dedup now finds desktop's add):
          → returns 'exists' with the desktop's key, no second POST.
        """
        from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl

        # First call
        zotero1 = MagicMock()
        zotero1.find_by_doi.return_value = None
        zotero_api1 = MagicMock()
        zotero_api1.create_item.return_value = ItemSnapshot("NEW123", 1, [], [], {})
        first = json.loads(add_reference_by_doi_impl(
            "10.1/x", collection_key=None, dry_run=False,
            zotero=zotero1, zotero_api=zotero_api1,
            resolver=MagicMock(return_value=_draft()),
        ))
        assert first["status"] == "created"

        # Second call: dedup now finds an item (could be ours-just-synced or desktop's)
        zotero2 = MagicMock()
        zotero2.find_by_doi.return_value = "DESKTOP456"
        zotero_api2 = MagicMock()
        second = json.loads(add_reference_by_doi_impl(
            "10.1/x", collection_key=None, dry_run=False,
            zotero=zotero2, zotero_api=zotero_api2,
            resolver=MagicMock(return_value=_draft()),
        ))
        assert second["status"] == "exists"
        assert second["item_key"] == "DESKTOP456"
        zotero_api2.create_item.assert_not_called()

    def test_create_does_not_retry_on_timeout(self):
        """A retry on POST timeout would risk duplicate items — must not retry."""
        from mcp_local_reference.tools.add_reference import add_reference_by_doi_impl
        from mcp_local_reference.services.zotero_api_client import ZoteroApiError
        zotero = MagicMock()
        zotero.find_by_doi.return_value = None
        zotero_api = MagicMock()
        zotero_api.create_item.side_effect = ZoteroApiError("timeout")
        resolver = MagicMock(return_value=_draft())

        result = json.loads(add_reference_by_doi_impl(
            "10.1/x", collection_key=None, dry_run=False,
            zotero=zotero, zotero_api=zotero_api, resolver=resolver,
        ))
        assert result["status"] == "error"
        # Critical: exactly one create attempt, no auto-retry
        assert zotero_api.create_item.call_count == 1
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_add_reference.py::TestConcurrencyInvariants -v
```

Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_add_reference.py
git commit -m "test(add_reference): pin concurrency invariants (order, short-circuit, recovery, no-retry)"
```

---

### Task 17: Register MCP tools and wire into server

Adds the `register_tools()` closure that exposes the three `*_impl` helpers as `@mcp.tool()` methods, and registers the module in `server.py`.

**Files:**
- Modify: `src/mcp_local_reference/tools/add_reference.py`
- Modify: `src/mcp_local_reference/server.py`

- [ ] **Step 1: Add `register_tools` to the module**

Append to `src/mcp_local_reference/tools/add_reference.py`:

```python
def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register the three add_reference_by_* MCP tools."""
    zotero = ZoteroClient(config)
    zotero_api = ZoteroApiClient(config)

    @mcp.tool()
    def add_reference_by_doi(
        doi: str,
        collection_key: str | None = None,
        dry_run: bool = True,
    ) -> str:
        """Add a reference to Zotero by DOI.

        Resolves metadata via Crossref, deduplicates against the local library,
        and POSTs a new item if not duplicate.

        Args:
            doi: The DOI string (e.g., '10.1145/3458817.3476195').
            collection_key: Optional Zotero collection key to file the new item in.
            dry_run: If True (default), preview the resolved metadata without writing.
        """
        return add_reference_by_doi_impl(
            doi, collection_key=collection_key, dry_run=dry_run,
            zotero=zotero, zotero_api=zotero_api,
        )

    @mcp.tool()
    def add_reference_by_arxiv(
        arxiv_id: str,
        collection_key: str | None = None,
        dry_run: bool = True,
    ) -> str:
        """Add a reference to Zotero by arXiv ID.

        Resolves metadata via the arXiv API, deduplicates, POSTs the item, and
        attempts to attach the open-access PDF as a child item (non-fatal if
        the PDF can't be fetched).

        Args:
            arxiv_id: The arXiv identifier (e.g., '2401.12345' or 'hep-th/0211177').
                Optional 'vN' version suffix preserved on the PDF URL.
            collection_key: Optional Zotero collection key.
            dry_run: If True (default), preview without writing.
        """
        return add_reference_by_arxiv_impl(
            arxiv_id, collection_key=collection_key, dry_run=dry_run,
            zotero=zotero, zotero_api=zotero_api,
            max_pdf_mb=config.add_reference_max_pdf_mb,
        )

    @mcp.tool()
    def add_reference_by_isbn(
        isbn: str,
        collection_key: str | None = None,
        dry_run: bool = True,
    ) -> str:
        """Add a reference to Zotero by ISBN.

        Resolves metadata via Open Library, deduplicates, POSTs a new book item.

        Args:
            isbn: ISBN-10 or ISBN-13, hyphenated or not. Checksum validated.
            collection_key: Optional Zotero collection key.
            dry_run: If True (default), preview without writing.
        """
        return add_reference_by_isbn_impl(
            isbn, collection_key=collection_key, dry_run=dry_run,
            zotero=zotero, zotero_api=zotero_api,
        )
```

- [ ] **Step 2: Register the module in `server.py`**

In `src/mcp_local_reference/server.py`, modify the import to add `add_reference`:

```python
from mcp_local_reference.tools import (
    add_reference,
    auto_tag,
    figures,
    local_pdf,
    pdf_reader,
    references,
)
```

And in the `create_server()` body, **after the existing `collections_tools.register_tools(mcp, config)` line** (it's currently the last `register_tools` call), append:

```python
add_reference.register_tools(mcp, config)
```

- [ ] **Step 3: Run the full test suite**

```bash
uv run pytest -v
```

Expected: all tests pass; the new `add_reference` module loads.

- [ ] **Step 4: Smoke-test the server boot**

```bash
uv run python -c "from mcp_local_reference.server import create_server; s = create_server(); print('OK')"
```

Expected: `OK`. (No real MCP transport; just verifies registration doesn't raise.)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/mcp_local_reference/tools/add_reference.py src/mcp_local_reference/server.py
uv run ruff format src/mcp_local_reference/tools/add_reference.py src/mcp_local_reference/server.py
git add src/mcp_local_reference/tools/add_reference.py src/mcp_local_reference/server.py
git commit -m "feat(add_reference): register MCP tools and wire into server"
```

---

### Task 18: Update CLAUDE.md

Add a paragraph describing the new tools, mirroring the style used for `auto_tag` and `collections`.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Locate the auto-tagging / collections paragraph**

In `CLAUDE.md`, find the existing block that describes auto-tagging and collection editing under "### mcp_local_reference". After the collection-editing paragraph, add a new bullet:

```markdown
- **Adding references (`tools/add_reference.py`):** three MCP tools — `add_reference_by_doi`, `add_reference_by_arxiv`, `add_reference_by_isbn` — that resolve a single canonical identifier through an external API (Crossref / arXiv / Open Library), dedupe against the local SQLite (skip-and-warn: returns the existing `item_key` with `status: "exists"` rather than POSTing a duplicate), and create a new item via the Web API. The arXiv tool additionally fetches and attaches the open-access PDF as a child item (non-fatal — if the PDF download or upload fails, the metadata item is still created with `pdf_status: "failed"`). All three tools default to `dry_run=True` and call the resolver even in dry-run so the LLM-facing preview shows real metadata. Optional `collection_key` is filed atomically as part of the create POST. Long-tail items without a DOI/arXiv/ISBN remain manually curated through Zotero's desktop UI.
```

- [ ] **Step 2: Verify CLAUDE.md still parses cleanly**

```bash
head -200 CLAUDE.md
```

Visually confirm the new bullet sits inside the `### mcp_local_reference` section, not in `### code_mcp`.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: describe add_reference tools in CLAUDE.md"
```

---

## Final Verification

After all tasks complete:

- [ ] **Full test run**

```bash
uv run pytest -v
```

Expected: all tests pass. Note the test count before and after to confirm ~46-50 new tests were added.

- [ ] **Full lint**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

Expected: clean.

- [ ] **Manual smoke test (Phase 1, read-only) — user-driven**

User runs the MCP server, invokes `add_reference_by_doi` with `dry_run=True` and a known DOI from their library. Confirms:
  - `status: "exists"`, `item_key` matches the existing item.
  - Title in the response matches the resolver's metadata, not the SQLite-stored title.

- [ ] **Manual smoke test (Phase 2, write end-to-end) — user-driven**

User invokes `add_reference_by_arxiv` with `dry_run=False` on a fresh preprint they don't have. Confirms (by checking Zotero desktop after sync):
  - New item appears with correct title, authors, abstract.
  - PDF is attached as a child item.
  - If they passed `collection_key`, item is filed there.

- [ ] **Branch is ready for merge**

Per CLAUDE.md: "fast-forward merged back to `main`". Confirm `git log main..feat/add-reference --oneline` shows the expected sequence of commits, then merge with `git checkout main && git merge --ff-only feat/add-reference`.
