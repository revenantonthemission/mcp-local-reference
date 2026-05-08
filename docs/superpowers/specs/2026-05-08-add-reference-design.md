# Add-Reference Design

**Date:** 2026-05-08
**Goal:** Add three MCP tools — `add_reference_by_doi`, `add_reference_by_arxiv`, `add_reference_by_isbn` — that resolve a single canonical identifier through an external metadata API, deduplicate against the local Zotero library, and POST a new bibliographic item to `api.zotero.org`. The arXiv tool additionally fetches and attaches the open-access PDF as a child item. Continues the read-SQLite / write-Web-API split established by `apply_tags` and the collection-editing toolkit.

## Problem

The existing write surface covers *editing* items the user already curated: tagging (`apply_tags`, `remove_tags`), collection lifecycle, and collection membership. There is no path to *create* a new bibliographic item from an MCP tool. Users currently fall back to Zotero's desktop UI or the Zotero Connector browser extension every time they encounter a paper while writing a blog post — a context switch that breaks the LLM-mediated workflow this project exists to support.

The library is bimodal (arXiv-classified AI/CS cluster + manually curated Sinology/Wittgenstein cluster). The AI/CS half — which dominates additions during blog writing — has near-universal identifier coverage (DOIs from publishers, arXiv IDs from preprints). The Sinology/Wittgenstein half includes books that ship with ISBNs (Hacker, Baker, Diamond on Wittgenstein; secondary literature on classical Chinese thought). The long-tail items without identifiers (untranslated classical texts, niche editions) remain manually curated through the desktop UI; this tool does not target them.

The shape of the risk is different from the editing tools. Editing tools mutate existing curated state and use optimistic concurrency to protect against silent overwrites. Creation tools are append-only at the API level, but introduce a new failure mode: silently polluting the library with duplicates. Skip-and-warn dedup is therefore the load-bearing safety primitive of this design — it replaces the role that `If-Unmodified-Since-Version` plays for edits.

## Decision Log

The following choices were made through clarifying questions during brainstorming. They are not open for re-litigation in implementation; they are the inputs.

| Question | Decision | Implication |
|---|---|---|
| Q1: Input shape — identifier vs. raw fields vs. hybrid? | A — identifier-only | LLM never authors Zotero schema; resolver is source of truth for citation fields. Long-tail items without identifiers stay in the desktop UI. |
| Q2: Which identifier types? | B — DOI + arXiv + ISBN | Three resolvers (Crossref, arXiv Atom, Open Library). No URL/translation-server dependency. |
| Q3: Duplicate handling? | A — skip-and-warn | If local SQLite finds the identifier, return existing `item_key` with `status: "exists"`. Never overwrite, never create twice. |
| Q4: PDF attachment? | B — arXiv auto-attach only | Predictable URL `arxiv.org/pdf/<id>.pdf`, no auth. DOI/ISBN remain metadata-only. |
| Q5: Collection placement on create? | A — optional `collection_key=None` | If provided, included in POST payload (single round-trip). If omitted, item lands at library root; user files later. |
| Q6: Tool surface — three tools vs. one with auto-detection? | Approach 1 — three tools | Each tool's signature documents its input format. Matches existing convention (`apply_tags` ≠ `remove_tags`; six separate collection-edit tools). |

## Design

### Tool Surface

Three new MCP tools in a new module `src/mcp_local_reference/tools/add_reference.py`:

```python
@mcp.tool()
def add_reference_by_doi(
    doi: str,
    collection_key: str | None = None,
    dry_run: bool = True,
) -> str: ...

@mcp.tool()
def add_reference_by_arxiv(
    arxiv_id: str,
    collection_key: str | None = None,
    dry_run: bool = True,
) -> str: ...

@mcp.tool()
def add_reference_by_isbn(
    isbn: str,
    collection_key: str | None = None,
    dry_run: bool = True,
) -> str: ...
```

`dry_run=True` is the default for all three, matching the project convention. Implementations live in module-level `*_impl()` helpers outside the `register_tools()` closure for direct unit-testability.

### Layering

```
tools/add_reference.py            ← MCP entry points (3 tools)
        │
        ▼
services/resolvers/               ← Stateless metadata fetchers
   __init__.py  (ZoteroItemDraft dataclass + ResolverError exceptions)
   crossref.py
   arxiv.py
   openlibrary.py
        │
        ▼
services/zotero_api_client.py     ← Extended with create_item() + upload_attachment()
        │
        ▼ (writes)
api.zotero.org

services/zotero_client.py         ← Extended with find_by_doi/arxiv_id/isbn (read-only SQLite, dedup)
        ▲ (reads)
        │
local Zotero SQLite (?mode=ro)
```

The local SQLite remains opened with `?mode=ro` end-to-end. Writes propagate to the local DB via Zotero's normal sync, preserving the existing read/write separation.

### Shared Type

```python
# services/resolvers/__init__.py

@dataclass(frozen=True)
class ZoteroItemDraft:
    """Resolver output — Zotero-shaped but not yet POSTed."""
    item_type: str          # "journalArticle" | "preprint" | "book" | "bookSection"
    fields: dict[str, Any]  # title, abstractNote, date, DOI, ISBN, url, publisher, ...
    creators: list[dict]    # [{"creatorType": "author", "firstName": ..., "lastName": ...}]
    pdf_url: str | None     # set only by arxiv resolver
    source_identifier: str  # echoed back in tool response for traceability


class ResolverError(RuntimeError):
    """Generic resolver failure — network, malformed response, etc."""

class ResolverNotFoundError(ResolverError):
    """Identifier not found in upstream metadata API (HTTP 404)."""
```

### Resolvers

| Module | Endpoint | Maps to Zotero `item_type` | PDF? |
|---|---|---|---|
| `crossref.py` | `GET api.crossref.org/works/{doi}` | `journalArticle` (default), `book`, `bookSection` based on Crossref `type` | no |
| `arxiv.py` | `GET export.arxiv.org/api/query?id_list={id}` | `preprint` | yes (`https://arxiv.org/pdf/{id}.pdf`) |
| `openlibrary.py` | `GET openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data` | `book` | no |

Each module exports one function: `resolve(identifier: str) -> ZoteroItemDraft`. All three are stateless, use the `httpx` client (already a dependency), and parse responses with stdlib facilities (`json` for Crossref/Open Library, `xml.etree` for arXiv Atom).

Each resolver retries once with 1-second backoff on 5xx / timeout. 404 raises `ResolverNotFoundError` immediately. Malformed responses (missing required fields like title) raise `ResolverError`.

### Dedup Layer (`services/zotero_client.py` extensions)

```python
def find_by_doi(self, doi: str) -> str | None: ...
def find_by_arxiv_id(self, arxiv_id: str) -> str | None: ...
def find_by_isbn(self, isbn: str) -> str | None: ...
```

All three: single SQL query joining `items` → `itemData` → `itemDataValues`, filtered by the appropriate `fieldID`. Implementation notes:

- **DOI lookup**: exact match against the DOI field.
- **arXiv lookup**: matches against the DOI field (`10.48550/arXiv.<id>` form) AND against the `extra` field (Zotero's `arXiv: <id>` convention for preprints). Handles `vN` suffixes by stripping the version before comparison.
- **ISBN lookup**: normalizes both the input and stored values (strip hyphens and spaces, uppercase trailing `X`) before comparing. Catches the common case of stored "978-0-07-352332-3" being queried as "9780073523323".

Returns the existing `item_key` if found, `None` otherwise.

### Write Layer (`services/zotero_api_client.py` extensions)

```python
def create_item(
    self,
    draft: ZoteroItemDraft,
    collection_key: str | None = None,
) -> ItemSnapshot:
    """POST /items with the draft serialized as a Zotero item template,
    plus optional collections array. Returns the snapshot of the new item.

    Follows the existing create_collection pattern: POST [payload],
    parse body['successful']['0'], raise ZoteroApiError on body['failed']."""

def upload_attachment(
    self,
    parent_key: str,
    pdf_bytes: bytes,
    filename: str,
) -> str:
    """Two-step Zotero upload (auth POST → S3 PUT → register).
    Returns the attachment item_key. Raises ZoteroApiError on any step.

    Caller decides whether to swallow (arXiv PDF case) or propagate."""
```

Both methods require credentials via the existing `_require_credentials()` guard. No new config keys.

### Tool Implementation Skeleton

```python
def add_reference_by_arxiv_impl(
    arxiv_id: str,
    collection_key: str | None,
    dry_run: bool,
    *,
    zotero: ZoteroClient,
    zotero_api: ZoteroApiClient,
    resolver: Callable[[str], ZoteroItemDraft] = arxiv.resolve,
    pdf_fetcher: Callable[[str, int], bytes] = _fetch_pdf,  # for tests
    max_pdf_mb: int = 50,
) -> dict:
    # 1. Validate identifier format
    if not _ARXIV_ID_RE.fullmatch(arxiv_id):
        return {"status": "error", "error": f"Invalid arXiv ID format: {arxiv_id}"}

    # 2. Resolve metadata
    try:
        draft = resolver(arxiv_id)
    except ResolverNotFoundError as e:
        return {"status": "error", "error": str(e)}
    except ResolverError as e:
        return {"status": "error", "error": str(e)}

    # 3. Dedup check (always, even in dry_run)
    existing = zotero.find_by_arxiv_id(arxiv_id)
    if existing is not None:
        return {
            "status": "exists",
            "item_key": existing,
            "title": draft.fields.get("title"),
            "warning": f"Already in library as {existing}",
            "dry_run": dry_run,
        }

    # 4. Dry-run preview
    if dry_run:
        return {
            "status": "would_create",
            "title": draft.fields.get("title"),
            "item_type": draft.item_type,
            "collection_key": collection_key,
            "pdf_status": "skipped",
            "dry_run": True,
        }

    # 5. Live create
    snapshot = zotero_api.create_item(draft, collection_key=collection_key)

    # 6. arXiv-only: PDF attachment (non-fatal)
    pdf_status = _attach_pdf(zotero_api, snapshot.item_key, draft.pdf_url, max_pdf_mb, pdf_fetcher)

    return {
        "status": "created",
        "item_key": snapshot.item_key,
        "title": draft.fields.get("title"),
        "item_type": draft.item_type,
        "collection_key": collection_key,
        "pdf_status": pdf_status,
        "dry_run": False,
    }
```

The DOI and ISBN tools follow the same shape minus the PDF step; their `pdf_status` is always `None`.

### Response Shape

Consistent across all three tools:

```python
{
    "status": "would_create" | "created" | "exists" | "error",
    "item_key": "ABC12345",          # always present except on error
    "title": "...",                  # for confirmation
    "item_type": "journalArticle",
    "collection_key": "XYZ789" | None,
    "pdf_status": "attached" | "skipped" | "failed" | None,  # None for DOI/ISBN
    "warning": "Already in library as ABC12345" | None,
    "dry_run": true | false,
    "error": "..."                   # only when status == "error"
}
```

### Configuration

One new config key on `Config`:

```python
add_reference_max_pdf_mb: int = 50
```

No new credentials. Existing `ZOTERO_USER_ID` + `ZOTERO_API_KEY` cover the write path. Crossref / arXiv / Open Library are unauthenticated.

## Data Flow

### Flow A: arXiv create (live, with PDF)

```
add_reference_by_arxiv("2401.12345", collection_key="ABC", dry_run=False)
  ├ arxiv.resolve("2401.12345")                    → ZoteroItemDraft (1 GET to export.arxiv.org)
  ├ zotero.find_by_arxiv_id("2401.12345")          → None  (local SQLite, indexed)
  ├ zotero_api.create_item(draft, "ABC")           → ItemSnapshot("NEW123", v=42) (1 POST)
  ├ _fetch_pdf("https://arxiv.org/pdf/2401.12345.pdf", 50)
  │     ├ HEAD/streaming GET — Content-Length check (skip if >50 MB)
  │     ├ download bytes
  │     ├ validate: bytes start with b"%PDF-" AND len(bytes) >= 1024
  │     └ return bytes  (1 GET to arxiv.org)
  ├ zotero_api.upload_attachment("NEW123", pdf_bytes, "2401.12345.pdf")  (3 calls: auth + S3 + register)
  └ return {"status": "created", "item_key": "NEW123", "pdf_status": "attached", ...}
```

### Flow B: DOI create (live, no PDF)

```
add_reference_by_doi("10.1145/3458817.3476195", collection_key=None, dry_run=False)
  ├ crossref.resolve(doi)                          → ZoteroItemDraft (pdf_url=None)
  ├ zotero.find_by_doi(doi)                        → None
  ├ zotero_api.create_item(draft, collection_key=None)
  └ return {"status": "created", "pdf_status": None, ...}
```

### Flow C: dry-run with existing item

```
add_reference_by_isbn("978-0-674-04207-2", dry_run=True)
  ├ normalize → "9780674042072"
  ├ openlibrary.resolve("9780674042072")           → ZoteroItemDraft  (1 GET to openlibrary.org)
  ├ zotero.find_by_isbn("9780674042072")           → "OLD456"
  └ return {"status": "exists", "item_key": "OLD456", "warning": "...", "dry_run": True}
```

The resolver is called even in dry-run, so the LLM-facing preview reflects real metadata rather than a guess. Cost: one external GET per dry-run invocation.

### Network Call Budget

| Step | DOI | arXiv | ISBN |
|---|---|---|---|
| Resolver fetch | 1 | 1 | 1 |
| Local dedup query | 0 | 0 | 0 |
| Item create POST | 1 | 1 | 1 |
| PDF download | — | 1 | — |
| PDF upload (auth + S3 + register) | — | 3 | — |
| **Total live requests** | **2** | **6** | **2** |
| **Dry-run requests** | **1** | **1** | **1** |

## Error Handling

Errors fall into four bands, applied consistently across all three tools.

### Band 1 — Input validation (pre-network)

| Check | Tool | On failure |
|---|---|---|
| DOI matches `10\.\d{4,9}/[^\s]+` | DOI | `{"status": "error", "error": "Invalid DOI format: ..."}` |
| arXiv ID matches `\d{4}\.\d{4,5}(v\d+)?` (new) or `[a-z\-]+/\d{7}` (old) | arXiv | Same shape |
| ISBN-10 or ISBN-13 checksum valid (after normalization) | ISBN | Same shape |

### Band 2 — Resolver failures

| Cause | Surface | LLM sees |
|---|---|---|
| Upstream 404 | `ResolverNotFoundError` | `{"status": "error", "error": "DOI ... not found in Crossref"}` |
| 5xx / timeout / conn reset | `ResolverError` after one 1s-backoff retry | `{"status": "error", "error": "Crossref unreachable: ..."}` |
| Malformed response (missing required fields) | `ResolverError` | `{"status": "error", "error": "Crossref returned no title for ..."}` |

### Band 3 — Zotero write failures

| Cause | Surface | LLM sees |
|---|---|---|
| Credentials unset | `MissingCredentialsError` (existing) | Same message as `apply_tags` already returns |
| `collection_key` doesn't exist | `ZoteroApiError` | `{"status": "error", "error": "Collection 'XYZ' not found"}`. **Item is not created** (atomic — collection sent in POST payload). |
| Rate limit (429) | `ZoteroApiError` carrying `retry_after` | LLM gets header value |
| Generic 5xx / timeout on POST | `ZoteroApiError` bubbles up; no auto-retry | LLM re-runs; dedup-on-rerun handles the case where the POST actually succeeded |

### Band 4 — PDF attachment failures (arXiv only, non-fatal)

The metadata item is already created and useful; the PDF is opportunistic.

| Cause | Behavior | Response field |
|---|---|---|
| arXiv PDF returns 404 (paper withdrawn) | Log warning; continue | `pdf_status: "failed"` |
| Network timeout on download | Log warning; continue | `pdf_status: "failed"` |
| `Content-Length > max_pdf_mb` | Skip download entirely (HEAD/streaming-GET short-circuit) | `pdf_status: "skipped"` |
| Magic-byte check fails (`bytes[:5] != b"%PDF-"`) — usually an HTML error page | Log captured content-type and first bytes; continue | `pdf_status: "failed"` |
| Min-size check fails (< 1024 bytes) | Log warning; continue | `pdf_status: "failed"` |
| Zotero attachment upload step fails | Log warning; continue | `pdf_status: "failed"` |

Magic-byte validation catches the silent-failure case where arXiv returns a 200 with an HTML error page (rare but real — happened during their 2023 storage migration). Without it, a renamed-`.pdf` HTML file would be uploaded and later choke the figures tool.

### Dedup race window

Between the local SQLite dedup check and the Zotero POST, the user could add the same item via Zotero desktop. We do not try to close this window. If a duplicate slips through, Zotero's web app surfaces it via its native "Duplicate Items" smart collection. A re-run of the same MCP tool returns `status: "exists"` against the now-synced local SQLite (dedup-on-rerun).

### Deliberately not done

- **Partial-write rollback.** Item created but PDF upload failed → leave the item created. Matches the `add_items_to_collection` partial-failure precedent.
- **Retry on writes.** Reads (resolvers) get one retry on 5xx. Writes (`create_item`, `upload_attachment`) get zero. Reasoning: a retried POST that succeeded twice creates a duplicate; a retried POST that succeeded once and timed out is the next dedup-skip on the next run. Both outcomes are recoverable by re-invoking the tool.
- **Identifier-cross-checking.** A Crossref-resolvable DOI of the form `10.48550/arXiv.2401.12345` is a `journalArticle` to the DOI tool and a `preprint` to the arXiv tool. The user picks the tool that names the intent.

## Testing

### Test Layout

```
tests/
  test_resolvers/
    test_crossref.py
    test_arxiv.py
    test_openlibrary.py
  test_add_reference.py
  fixtures/resolvers/
    crossref/<doi-encoded>.json
    arxiv/<id>.xml
    openlibrary/<isbn>.json
```

### Test Coverage Plan

| Layer | What to test | Approach |
|---|---|---|
| Resolvers (3 modules) | Happy path + 404 + malformed response per identifier type. Field mapping (title, authors, date, abstract, item_type). ISBN normalization (hyphens, X). | Captured response fixtures fed to `httpx.MockTransport`. No live network. |
| `zotero_client.find_by_*` (3 methods) | DOI hits/misses; arXiv via DOI field AND via extra field; ISBN normalized comparison | Extends existing `tests/conftest.py` mock SQLite — add items with known DOIs/arXiv IDs/ISBNs |
| `zotero_api_client.create_item` | Successful POST → ItemSnapshot; `body["failed"]` → `ZoteroApiError`; `collection_key` included in payload when provided | `httpx.MockTransport` |
| `zotero_api_client.upload_attachment` | Three-step flow (auth POST → S3 PUT → register); each step's failure surfaces distinctly | `httpx.MockTransport` with sequential expectations |
| `add_reference_by_*_impl` (3 tools) | dry_run-not-exists, dry_run-exists, live-not-exists, live-exists, resolver-error, missing-credentials. Plus arXiv-specific PDF tests below. | Injected dependencies — no HTTP at this level |

### Concurrency Tests (dedup-check ↔ POST race)

| Test | What it asserts |
|---|---|
| `test_dedup_check_runs_before_create` | Via `mock_calls`, assert `find_by_doi` is called before `create_item`. Catches accidental refactor reordering. |
| `test_existing_item_blocks_create_call` | When `find_by_doi` returns a key, `create_item` is never called. |
| `test_recovery_after_concurrent_add` | First call: create succeeds. Second call (with `find_by_doi` now returning the desktop-added item_key): returns `status: "exists"`, no second POST. Pins the dedup-on-rerun contract. |
| `test_create_does_not_retry_on_timeout` | `create_item` raises `httpx.TimeoutException` → tool surfaces error and does NOT issue a second POST. |

These pin the four invariants that make the recovery story sound: order, short-circuit, idempotent rerun, no-retry. They do not (and cannot, without integration infra) test a true cross-process race.

### PDF Binary-Content Tests

| Test | Body fixture | Expected outcome |
|---|---|---|
| `test_pdf_valid_magic_bytes` | `b"%PDF-1.7\n..." + b"x" * 2000` | `pdf_status: "attached"`, `upload_attachment` called once |
| `test_pdf_html_error_page_rejected` | `b"<!DOCTYPE html>...arXiv error..."` | `pdf_status: "failed"`, `upload_attachment` NOT called, item still created |
| `test_pdf_too_small_rejected` | `b"%PDF-1.7\nempty"` (12 bytes) | `pdf_status: "failed"`, `upload_attachment` NOT called |
| `test_pdf_oversize_skipped` | Mock `Content-Length: 53477376` | `pdf_status: "skipped"`, no body downloaded past header |
| `test_pdf_upload_failure_does_not_roll_back_item` | Valid PDF; `upload_attachment` raises `ZoteroApiError` | Item still in `status: "created"`, `pdf_status: "failed"` |
| `test_pdf_truncated_during_download_rejected` | `httpx.MockTransport` raises mid-stream | `pdf_status: "failed"`, no upload, item still created |

### Captured Fixtures

A small set of real-world payloads, committed once and reused:

- **Crossref**: one journal article with full creators, one with corporate author, one 404, one with `type: "book-chapter"` (maps to `bookSection`).
- **arXiv**: one new-style ID (`2401.12345`), one old-style (`hep-th/0211177`), one with multiple versions (`2401.12345v3`).
- **Open Library**: one ISBN-13 hit, one ISBN-10 hit, one 404, one with sparse metadata (no publisher).

Captured via `curl > tests/fixtures/resolvers/...` once. Catches resolver field-mapping regressions without network access in CI.

### Test Count

~46–50 tests total. Distribution: ~9 resolvers × 3 + ~8 dedup + ~4 create_item + ~4 upload_attachment + ~6 concurrency/PDF + ~12 tool-orchestration = ~50.

### What we don't test

- True multi-process race against a live Zotero account — needs integration harness, covered by manual user validation per the project's Phase 1 (read) → Phase 2 (write) discipline.
- Whether the PDF semantically matches the requested arXiv paper — the URL pattern `arxiv.org/pdf/<id>.pdf` is provenance enough.

## Open Questions / Out of Scope

- **URL-based adds via Zotero translation server.** Ruled out in Q2 (`(B)` chosen over `(C)`). Could be added later as a fourth tool `add_reference_by_url` if blog-post / web-article additions become a frequent motion.
- **Sci-Hub / paywall PDF retrieval.** Out of scope. Only arXiv's predictable open-access URL is automated.
- **Bulk-add tooling.** This spec is one-identifier-at-a-time. A future `add_references_batch` could wrap the three impl helpers if batch-grazing arXiv listings becomes painful, but YAGNI for now — the LLM can chain calls.
- **DOI → arXiv cross-detection.** A DOI of the form `10.48550/arXiv.<id>` is treated as a journal article by the DOI tool. Users wanting a `preprint` should call the arXiv tool directly. Cross-detection logic would add ambiguity for marginal benefit.
