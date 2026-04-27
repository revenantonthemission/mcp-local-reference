# Collection Editing Design

**Date:** 2026-04-27
**Goal:** Add MCP tools for editing Zotero collections — both **collection-object lifecycle** (create / rename / re-parent / delete) and **item-to-collection membership** (add / remove items per collection) — plus a read-only `suggest_collection_placement` tool that gives Claude grounded context to advise on placement without writing. Builds on the same Web-API + dry-run + optimistic-concurrency pattern established by `apply_tags` / `remove_tags`.

## Problem

The auto-tagging family (`suggest_tags_context`, `apply_tags`, `remove_tags`) covers tag mutations but stops short of organizational structure. Collection edits — moving a misfiled paper into the right subfolder, renaming a folder whose name no longer reflects its contents, deleting a stale folder, re-parenting a folder that grew into a subtopic — currently require opening the Zotero desktop UI. The `tag-removal` spec (2026-04-27) explicitly deferred this work (Q2: E) and left it to a future spec; this is that spec.

The user's library is bimodal (CS/AI cluster + Sinology/Wittgenstein cluster), which makes Claude's judgment on "where does this paper belong?" genuinely useful — a paper's abstract usually screams which side it belongs on. But Claude-judgment *writes* are the same risk shape as Claude-judgment tag removals: irreversible (collection-membership history is not preserved through delete) and easy to get wrong silently. So this spec keeps writes user-directed (the same Phase A discipline as `remove_tags`) while adding one new read tool that Claude can call to gather context before *advising* the user in chat.

## Decision Log

The following choices were made through clarifying questions during brainstorming. They are not open for re-litigation in implementation; they are the inputs.

| Question | Decision | Implication |
|---|---|---|
| Q1: Which kind of collection edit? | C — both surfaces (lifecycle + item membership) designed together | One spec covers all 7 tools; implementation plan may slice into reviewable phases. |
| Q2: What's the pain you're fixing? | D — mixed cleanup, one-off pass, no specific high-frequency motion | Toolkit shape (small set of careful primitives) rather than one flagship optimized tool. Strong dry-run defaults. |
| Q3: Who decides target + destination? | C — hybrid: writes are user-directed, a separate read tool gives Claude advisory context | Adds `suggest_collection_placement` as a sibling of `suggest_tags_context`. Writes never auto-propose. |
| Q4: Lifecycle subset? | A — all four (create, rename, re-parent, delete) | Full lifecycle surface, including cycle detection on re-parent. |
| Q5: Item-membership shape? | A — append-only siblings (`add_items_to_collection` + `remove_items_from_collection`) | Mirrors `apply_tags` / `remove_tags` exactly. "Move" = remove from A, then add to B (two preview-confirm cycles). |
| Q6: File structure? | Approach 1 — single tool module + extend existing `ZoteroApiClient` | Mirrors the proven `auto_tag.py` pattern. No new service files. |
| Q7: `delete_collection` safety stance? | A — dry-run preview only, no extra `force` flag | Matches `remove_tags` precedent: "dry_run is the safety gate." Preview enumerates items and child collections that would be orphaned. |

## Design

### Tool Surface

Seven new MCP tools in a new module `src/mcp_local_reference/tools/collections.py`:

```python
# Lifecycle (4)
@mcp.tool()
def create_collection(name: str, parent_key: str | None = None, dry_run: bool = True) -> str: ...

@mcp.tool()
def rename_collection(collection_key: str, new_name: str, dry_run: bool = True) -> str: ...

@mcp.tool()
def reparent_collection(
    collection_key: str,
    new_parent_key: str | None,   # None = move to library root
    dry_run: bool = True,
) -> str: ...

@mcp.tool()
def delete_collection(collection_key: str, dry_run: bool = True) -> str: ...

# Item membership (2)
@mcp.tool()
def add_items_to_collection(
    collection_key: str,
    item_keys: list[str],
    dry_run: bool = True,
) -> str: ...

@mcp.tool()
def remove_items_from_collection(
    collection_key: str,
    item_keys: list[str],
    dry_run: bool = True,
) -> str: ...

# Suggestion (1, read-only)
@mcp.tool()
def suggest_collection_placement(item_key: str) -> str: ...
```

Conventions inherited from `auto_tag.py` (no changes):

- Every write tool defaults `dry_run=True` and returns a JSON string.
- Every write tool has a module-level `*_impl()` helper for direct unit testing without an MCP harness.
- Item-membership tools enforce `MAX_ITEMS_PER_CALL = 25` (mirrors `MAX_TAGS_PER_CALL`).
- Web API writes use `If-Unmodified-Since-Version` for optimistic concurrency.
- Dry-run reads from the local SQLite (no creds needed); writes re-fetch from the Web API for the authoritative version.

Two intentional asymmetries with `auto_tag.py`:

1. Lifecycle tools operate on **collection** records (`/collections/<key>` versioning), not item records. Different endpoint family, same concurrency story.
2. `add_items_to_collection` and `remove_items_from_collection` patch *each item's* `collections` array. A 25-item batch becomes **25 PATCH calls**, each with its own version check. This expands the partial-failure surface (covered in §Data Flow — Family B).

### Service Layer Extensions

All new methods land on the existing `ZoteroApiClient` in `services/zotero_api_client.py`. No new files. The existing `_client()`, `_headers()`, `_require_credentials()` and the existing exception types (`ZoteroApiError`, `MissingCredentialsError`, `VersionConflictError`) are reused unchanged.

```python
# Collection-object operations
def create_collection(self, name: str, parent_key: str | None) -> CollectionSnapshot: ...
def get_collection(self, collection_key: str) -> CollectionSnapshot: ...
def update_collection(
    self,
    collection_key: str,
    *,
    name: str | None = None,
    parent_key: str | None | _Sentinel = _UNSET,   # explicit None = move to root
    version: int,
) -> int: ...
def delete_collection(self, collection_key: str, version: int) -> None: ...

# Item-record collection-membership write (sibling of set_tags)
def update_item_collections(
    self,
    item_key: str,
    collection_keys: list[str],
    version: int,
) -> int: ...
```

A new `CollectionSnapshot` dataclass mirrors `ItemSnapshot`:

```python
@dataclass
class CollectionSnapshot:
    collection_key: str
    version: int
    name: str
    parent_key: str | None   # None at root
    raw: dict[str, Any]
```

`ItemSnapshot` gains a backward-compatible `collections: list[str]` field populated from the API's `data.get("collections", [])`. `auto_tag.py` ignores the new field; no existing call site breaks.

**The `_UNSET` sentinel.** `update_collection` needs three-state semantics for `parent_key`:

- omitted (`_UNSET`) → don't touch parent (used by `rename_collection`)
- explicit `None` → move to library root (Web API: `parentCollection: false`)
- explicit string → move under that collection

Without the sentinel you can't distinguish "don't touch" from "move to root." The sentinel is module-private (`_UNSET`); callers pass `None` or a string only.

`services/zotero_client.py` (read-side) gains one helper:

```python
def get_item_collections(self, item_key: str) -> list[str]:
    """Return the collection keys an item currently belongs to (local SQLite)."""
```

This is needed so the membership-tool dry-run paths can partition `would_add` / `already_present` / `would_remove` / `not_present` without contacting the Web API.

### Data Flow — Family A: Lifecycle (create, rename, reparent, delete)

**Dry-run path (no creds needed):**

```
tool(target, params, dry_run=True)
  │
  ├─ validate inputs (non-empty name; parent exists locally; no cycle for reparent; etc.)
  ├─ snapshot ← _local_collection_snapshot(zotero, key)   # for rename/reparent/delete
  │            (skipped for create — nothing exists yet)
  ├─ compute would_<verb> diff (new state vs current state)
  └─ return {"status": "preview", "would_<verb>": ..., "current": ..., "after": ...}
```

**Write path:**

```
tool(target, params, dry_run=False)
  │
  ├─ validate
  ├─ snapshot ← api.get_collection(key)   # authoritative version (skipped for create)
  ├─ if no-op (rename to same name; reparent to same parent): return {"status": "no_changes"}
  ├─ api.<create|update|delete>_collection(...)
  └─ return {"status": "applied", "new_version": ..., ...}
```

Per-tool deviations:

- **`create_collection`** has no version snapshot to fetch. Idempotency: if a collection with the same `(name, parent_key)` already exists locally, dry-run reports `status: "already_exists"` with the existing key; write refuses with the same payload (no POST, no duplicate creation).
- **`reparent_collection`** dry-run computes the proposed parent chain and detects the cycle case (new parent is the target itself or a descendant) — returns `{"error": "Cycle detected: collection X cannot be re-parented under its own descendant Y"}` with no API call.
- **`delete_collection`** dry-run enumerates blast: `would_orphan_items` (items losing membership), `would_orphan_collections` (sub-collections whose `parentCollection` points to the target). The apply path proceeds regardless of size (Q7: A — `dry_run` is the safety gate).

### Data Flow — Family B: Item-membership (add, remove)

The 25-batch-of-PATCHes story.

**Dry-run path:**

```
add_items_to_collection(coll_key, item_keys, dry_run=True)
  │
  ├─ validate: 0 < len(item_keys) ≤ MAX_ITEMS_PER_CALL
  ├─ resolve coll_key locally (must exist)
  ├─ for each item_key: read its current `collections` list from local SQLite
  ├─ partition:
  │     would_add        = items NOT currently in coll
  │     already_present  = items currently in coll
  │     not_found        = items missing from local SQLite
  └─ return {"status": "preview", "would_add": [...], "already_present": [...], "not_found": [...]}
```

**Write path — partial-failure-aware:**

```
add_items_to_collection(coll_key, item_keys, dry_run=False)
  │
  ├─ same validation + local partition (already_present and not_found skip the PATCH loop)
  ├─ for each item in would_add (sequentially):
  │     try:
  │       snap ← api.get_item(item)
  │       new_colls ← snap.collections ∪ {coll_key}
  │       api.update_item_collections(item, new_colls, snap.version)
  │       record success(item, new_version)
  │     except (VersionConflictError, ZoteroApiError) as e:
  │       record failure(item, reason=str(e))
  │       continue  # do NOT abort the batch
  └─ return {
       "status":           "applied" | "partial",  # partial if any failure
       "succeeded":        [{item_key, new_version}, ...],
       "failed":           [{item_key, reason},     ...],
       "already_present":  [...],
       "not_found":        [...],
     }
```

`remove_items_from_collection` mirrors this with `would_remove` / `not_present` and `new_colls = snap.collections − {coll_key}`.

**Why sequential, not parallel?** Three reasons: each PATCH is independent (different items, different versions); Zotero rate-limits aggressively; sequential output is easier to reason about in a partial-failure preview. If 25 PATCHes is too slow in practice, batching can be added in a future spec — YAGNI for now.

### Data Flow — Family C: Suggest (read-only)

```
suggest_collection_placement(item_key)
  │
  ├─ load item: title, abstract (or PDF first-page snippet, same fallback as suggest_tags_context)
  ├─ load full collection tree (zotero.list_collections())
  ├─ load item's current collections (which folders is it in now?)
  └─ return JSON {
       "item": {title, abstract_or_snippet, current_collections: [{key, name, path}, ...]},
       "collection_tree": [...],   # nested as in list_collections
       "vocabulary": {...}         # see below
     }
```

**Vocabulary anchoring** — analogous to `suggest_tags_context`'s top-30 tags: for each collection, include its **item count** in the response. A single SQLite aggregate query (`COUNT` per `collectionID`); no semantic embedding needed. Claude can then prefer well-populated, established folders over thinly-used ones when advising the user.

The tool has **no write path**. The user is expected to read the response, reason in chat, then invoke the appropriate write tool with explicit collection + item keys.

### Edge Cases

| Tool | Edge case | Behavior |
|---|---|---|
| `create_collection` | Name already exists at same parent | Local SQLite check → `{"status": "already_exists", "existing_key": "..."}`. No API call. |
| `create_collection` | `parent_key` doesn't exist locally | `{"error": "Parent collection 'XYZ' not found"}`. Fail fast. |
| `rename_collection` | New name same as current | `{"status": "no_changes"}` short-circuit. |
| `rename_collection` | New name collides with sibling under same parent | `{"error": "A collection named 'X' already exists under this parent"}`. Local check, no API call. (Zotero itself permits this; we refuse for hygiene.) |
| `reparent_collection` | New parent equals current parent | `{"status": "no_changes"}`. |
| `reparent_collection` | New parent is target itself or a descendant | `{"error": "Cycle detected: ..."}`. Local DAG walk on the cached tree. |
| `reparent_collection` | New parent doesn't exist locally | `{"error": "Parent collection 'XYZ' not found"}`. |
| `delete_collection` | Collection doesn't exist locally | `{"error": "Reference '<key>' not found"}` (same wording as `apply_tags` for missing items). |
| `delete_collection` | Has items / child collections | Dry-run reports `would_orphan_items`, `would_orphan_collections`. Apply proceeds. |
| `add_items_to_collection` | `coll_key` doesn't exist locally | Fail fast before any per-item PATCH. |
| `add/remove_items_from_collection` | Empty `item_keys` list | `{"error": "No item keys provided"}`. |
| `add/remove_items_from_collection` | More than 25 items | `{"error": "Refusing: N items exceeds the per-call cap of 25..."}`. Mirrors `apply_tags` wording. |

### Response Payloads

Status values across all tools: `"preview"` (dry-run), `"applied"` (full success), `"partial"` (membership only — some succeeded, some failed), `"no_changes"` (write short-circuit), `"already_exists"` (create only). Plus standard `{"error": "..."}` payloads which omit `status`.

**Lifecycle dry-run** (rename example):

```json
{
  "collection_key": "ABC12345",
  "current": {"name": "AI", "parent_key": null},
  "would_rename_to": "Artificial Intelligence",
  "after": {"name": "Artificial Intelligence", "parent_key": null},
  "dry_run": true,
  "status": "preview"
}
```

**Lifecycle applied** (delete example):

```json
{
  "collection_key": "ABC12345",
  "deleted": {"name": "Old Folder", "parent_key": "DEF67890"},
  "would_orphan_items": ["KEYAAAAA", "KEYBBBBB"],
  "would_orphan_collections": [],
  "dry_run": false,
  "status": "applied"
}
```

**Membership applied (partial):**

```json
{
  "collection_key": "ABC12345",
  "succeeded": [
    {"item_key": "KEY1", "new_version": 4521},
    {"item_key": "KEY2", "new_version": 4522}
  ],
  "failed": [
    {"item_key": "KEY3", "reason": "Item 'KEY3' was modified since version 4500"}
  ],
  "already_present": ["KEY4"],
  "not_found": [],
  "dry_run": false,
  "status": "partial"
}
```

### Error Handling

Mirrors the tag-removal spec format. Every failure mode below has a corresponding test (see Testing).

| # | Condition | Detected at | Tools affected | Response |
|---|---|---|---|---|
| 1 | Empty/whitespace input list (item_keys, name) | Input validation | All write tools | `{"error": "..."}` |
| 2 | Batch size > 25 (item_keys) | Input validation | `add/remove_items_from_collection` | `{"error": "Refusing: N items exceeds the per-call cap of 25..."}` |
| 3 | Target collection missing locally (dry_run only) | `_local_collection_snapshot` | rename, reparent, delete, add/remove_items | `{"error": "Reference '<key>' not found"}` |
| 4 | Parent collection missing locally | Local tree lookup | create, reparent | `{"error": "Parent collection 'X' not found"}` |
| 5 | Cycle detected (reparent) | Local DAG walk | reparent | `{"error": "Cycle detected: ..."}` |
| 6 | Sibling-name collision | Local lookup | create, rename | `{"error": "A collection named 'X' already exists under this parent"}` |
| 7 | Missing creds (write only) | First `api.*` call → `MissingCredentialsError` | All writes | Standard creds error wording |
| 8 | Target missing in Web API (write only) | `api.get_*()` 404 → `ZoteroApiError` | All writes except create | `{"error": "...not found in Zotero Web API"}` |
| 9 | All proposed items skipped (write only) | Post-diff short-circuit | add/remove_items | `{"status": "no_changes", ...}` no PATCH |
| 10 | Version conflict (HTTP 412) | `api.update_*` raises `VersionConflictError` | Lifecycle: error w/ retry hint. Membership: per-item failure, batch continues. |
| 11 | Network / 5xx / other HTTP | httpx → `ZoteroApiError` | All writes | `{"error": "<HTTP error>"}` for lifecycle; per-item failure for membership |

### Asymmetry with `apply_tags` / `remove_tags`, called out explicitly

`apply_tags` and `remove_tags` are individually atomic — one PATCH per call, success or full version-conflict failure, no in-between. The new item-membership tools are **per-item atomic** but **batch-non-atomic**: in a 25-item add, items 1–6 may apply before item 7 hits a version conflict and is recorded as `failed`, with items 8–25 still attempted. The user gets a complete `succeeded[] + failed[]` payload describing exactly what landed and what didn't.

Mitigations against this expanded blast radius:

- `dry_run=True` default — the user sees the full set of proposed PATCHes before any write.
- `MAX_ITEMS_PER_CALL = 25` — bounds the maximum partial-failure size.
- Per-item failure rows include the item key and reason; the user can re-run the failed subset after the underlying conflict clears.
- Sequential execution (not parallel) keeps the failure log linear and reasoning-friendly.

## Testing

New `tests/test_collections.py`. Reuses existing helpers: `_FakeApi` (extended with collection methods), `_FakeZotero` (extended with `list_collections` and `get_item_collections` stubs), `mock_zotero_db` fixture, `tmp_dir`, `api_config`.

| Test class | Focus | Tests |
|---|---|---|
| `TestCreateCollection` | dry-run, write, parent-missing, sibling-collision, idempotency on already-exists, missing-creds | 6 |
| `TestRenameCollection` | dry-run, write, no-op, sibling-collision, version conflict, missing-locally | 6 |
| `TestReparentCollection` | dry-run, write, no-op, parent-missing, cycle-to-self, cycle-to-descendant, version conflict | 7 |
| `TestDeleteCollection` | dry-run-empty, dry-run-with-items, dry-run-with-children, write-applied, write-version-conflict, missing-locally | 6 |
| `TestAddItemsToCollection` | dry-run, write-clean, write-partial-failure, all-already-present (no-changes), cap, missing-coll, missing-creds | 7 |
| `TestRemoveItemsFromCollection` | dry-run, write-clean, write-partial-failure, all-not-present (no-changes), cap | 5 |
| `TestSuggestCollectionPlacement` | returns title+abstract+tree+counts, abstract fallback to PDF snippet, missing-item error | 3 |
| `TestZoteroApiClient` (extension to existing) | `create_collection` POST, `update_collection` PATCH (rename, reparent, both), `delete_collection` DELETE, `update_item_collections` PATCH (each with version-header behavior) | 6 |
| **Total** | | **+46** |

Existing 150 tests stay green; suite goes 150 → 196.

### What we deliberately don't re-test

- `_local_snapshot` for items — already covered in `test_auto_tag.py`.
- `If-Unmodified-Since-Version` header behavior in isolation — already covered by `test_set_tags_sends_patch_with_version_header`. New `update_*` methods reuse the same plumbing path; we trust it once.
- ChromaDB / vector store — collection edits don't touch that subsystem.
- `ZoteroClient.list_collections` — already covered in `test_zotero_client.py`. We reuse it via `_FakeZotero`.

### Smoke Test

After the suite is green, an end-to-end script (parallel to the `apply_tags` smoke) runs against the real Zotero library:

1. `create_collection("__claude_smoke_test__")` → capture `key`
2. `rename_collection(key, "__claude_smoke_test_renamed__")`
3. `reparent_collection(key, <existing-test-parent>)` then back to root (`new_parent_key=None`)
4. `add_items_to_collection(key, [some_item])`
5. `remove_items_from_collection(key, [some_item])`
6. `delete_collection(key)`
7. Re-fetch collection list via local SQLite (after Zotero sync) → confirm gone

This validates real-API behavior the mocks can't fully exercise — particularly `parentCollection: false` semantics for "move to root" and the local-sync round-trip after each write.

## Out of Scope

| Excluded | Why |
|---|---|
| Bulk-move-between-collections atomic tool (`move_items_between_collections`) | Q5: A — covered by `remove` then `add` siblings; adds tool surface without new capability. |
| Claude-judgment writes (Claude proposes a move, then auto-applies) | Q3: C — write tools are user-directed only; the suggest tool exists for *advice* the user then acts on. |
| Cross-library (group library) collections | Existing `ZoteroApiClient` uses `/users/<id>/`. Group libraries (`/groups/<id>/`) live in a future spec. |
| Recursive delete (delete a folder + all descendants in one call) | Q7: A keeps `delete_collection` to one target. Cleanup of subtrees is an explicit leaf-up sequence the user runs. |
| "Did you mean" / fuzzy-match suggestions on names | Same reasoning as `remove_tags` — strict equality avoids the synonym-sprawl bug. |
| Transactional multi-step lifecycle (rename + reparent in one call) | Two calls, two version checks, two preview cycles is the safer pattern. The extra friction matches the Q2: D "mixed cleanup, careful pass" workflow. |
| Saved searches / smart collections | Different Zotero data model (`savedSearches` table, not `collections`); separate spec. |

## Success Criteria

1. All +46 new tests pass; existing 150 stay green (196 total).
2. `ruff check src/ tests/` clean; `ruff format` clean.
3. Smoke test against the real Zotero library completes the 7-step sequence above with each step's response verified against an immediate API re-fetch.
4. `CLAUDE.md` describes collection-editing tools at the same level of detail as the existing auto-tagging paragraph.
5. New tools follow the codebase conventions: `*_impl()` helpers outside the closure, `dry_run=True` default, `MAX_ITEMS_PER_CALL = 25`, optimistic concurrency via `If-Unmodified-Since-Version` on every write, the `_UNSET` sentinel only where the three-state semantics actually need it.

## Files Touched

| File | Change |
|---|---|
| `src/mcp_local_reference/tools/collections.py` | **NEW** — 6 write tools + `suggest_collection_placement` + their `*_impl()` helpers + private `_local_collection_snapshot`, `_walk_descendants`, `_check_cycle` helpers |
| `src/mcp_local_reference/services/zotero_api_client.py` | + `CollectionSnapshot` dataclass; + `_UNSET` sentinel; + `create_collection`, `get_collection`, `update_collection`, `delete_collection`, `update_item_collections` methods; `ItemSnapshot.collections` field added |
| `src/mcp_local_reference/services/zotero_client.py` | + `get_item_collections(item_key) -> list[str]` helper for dry-run partition |
| `src/mcp_local_reference/server.py` | + `register_tools(mcp, config)` call for `tools.collections` |
| `tests/test_collections.py` | **NEW** — 7 test classes, 40 tool-level tests |
| `tests/test_zotero_api_client.py` | + 6 tests for the new client methods |
| `CLAUDE.md` | Expand the auto-tagging paragraph into a "Collection editing" paragraph that lists all 7 tools and references the same dry-run / cap / concurrency pattern |

No changes to `config.py`, `pyproject.toml`, `vector_store.py`, `pdf_processor.py`, or any `code_mcp/*` file.
