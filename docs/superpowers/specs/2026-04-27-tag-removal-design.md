# Tag Removal Design

**Date:** 2026-04-27
**Goal:** Add a third MCP tool, `remove_tags`, to the auto-tagging family so the user can correct tags that earlier auto-tagging or import workflows added incorrectly. The existing `apply_tags` is append-only; this is its inverse.

## Problem

`apply_tags` was deliberately append-only — it can never remove a user-curated tag. After running it on real items, the user observed semantically-inappropriate tags (Q1: A) that need to be undone. There is currently no MCP-level primitive to remove a tag from an item; the only path is opening the Zotero desktop UI manually.

A user-directed write tool (Q3: A — Claude does not judge what to remove; the user names the targets) is the urgent need. Library-wide rename and Claude-suggested-removal are deferred (Q3: C means ship A, mark B in the spec).

Collection editing was scoped out of this design (Q2: E) and lives in a future spec.

## Decision Log

The following choices were made through clarifying questions during brainstorming. They are not open for re-litigation in implementation; they are the inputs.

| Question | Decision | Implication |
|---|---|---|
| Q1: What kind of mistakes drove this? | A — wrong tag, semantically inappropriate | Single primitive: targeted per-item removal. No library-wide rename, no full-replace. |
| Q2: What's the collection-editing scenario? | E — skip collections this round | No collection tools. Deferred to a separate spec if needed. |
| Q3: How should removal decisions be made? | C — ship user-directed (A) now, mark Claude-suggested (B) as deferred | Phase A: write tool only. Phase B: future read tool that flags suspicious tags. |
| Q4: When a tag isn't on the item? | A — silent no-op for that tag, report under `not_present` | Idempotent. Mirrors `apply_tags`'s `already_present` handling. |
| Architecture | Approach 1 — sibling tool parallel to `apply_tags` | New `remove_tags` MCP tool + `remove_tags_impl` module-level function. No changes to existing `apply_tags` API or tests. |

## Design

### Tool Surface

One new MCP tool registered alongside `suggest_tags_context` and `apply_tags`:

```python
@mcp.tool()
def remove_tags(item_key: str, tags: list[str], dry_run: bool = True) -> str:
    """Remove tags from a Zotero item via the Web API.

    Mirrors apply_tags but in reverse: tags listed here are *removed*
    from the item's tag list. Defaults to dry_run=True. Idempotent —
    tags not present on the item are silently skipped and reported
    under not_present in the response. Per-call cap matches apply_tags
    (25) to guard against runaway batch destruction.

    Designed for fixing tags that auto-tagging or import workflows
    added incorrectly. Should NOT be used on Claude's own judgment —
    only invoke when the user explicitly identifies tags to remove.
    """
    return remove_tags_impl(api, zotero, item_key, tags, dry_run)
```

The closure passes the same `api` (`ZoteroApiClient`) and `zotero` (`ZoteroClient`) instances `apply_tags` already uses. No new services, no new env vars, no Config changes.

The implementation lives in a module-level `remove_tags_impl` function so it's directly unit-testable without an MCP harness — same discipline as the existing `apply_tags_impl` and `suggest_tags_context_impl`.

### Composition with existing tools

| Tool | Direction | When called |
|---|---|---|
| `suggest_tags_context` | read | Claude reasons about *what* to tag |
| `apply_tags` | write (add) | After Claude proposes additions |
| `remove_tags` | write (remove) | When the user names mistakes to undo |

### Data Flow — Dry-run path (no creds needed)

```
remove_tags(item_key, tags, dry_run=True)
  │
  ├─ validate: non-empty, len ≤ MAX_TAGS_PER_CALL (25)
  ├─ snapshot = _local_snapshot(zotero, item_key)    # local SQLite, version=0
  ├─ would_remove  = proposed ∩ current_tags         # actually on the item
  ├─ not_present   = proposed - current_tags         # silently skipped
  ├─ after_apply   = current_tags - proposed         # what would remain
  └─ return {"status": "preview", ...}               # never touches the API
```

The dry-run path reuses the existing `_local_snapshot` helper introduced for `apply_tags`. No new private helpers are needed for the dry-run side.

### Data Flow — Write path

```
remove_tags(item_key, tags, dry_run=False)
  │
  ├─ validate
  ├─ snapshot = api.get_item(item_key)               # Web API, authoritative version
  ├─ compute would_remove / not_present / after_apply (same as dry-run)
  ├─ if would_remove is empty:
  │     return {"status": "no_changes", ...}         # short-circuit, no PATCH
  ├─ api.set_tags(item_key, after_apply, snapshot.version)
  │   ↳ Zotero PATCHes the tags array with after_apply (current minus removed)
  │   ↳ If-Unmodified-Since-Version: <version>
  └─ return {"status": "applied", "new_version": ..., "removed_count": ...}
```

**Key reuse:** `ZoteroApiClient.set_tags(item_key, full_tag_list, version)` is unchanged — it already PATCHes with whatever full tag list is passed. Removal is "give it the kept tags." No new HTTP method, no new client primitive.

### Response Payload

Mirrors `apply_tags` field-for-field, with names that match the operation:

| `apply_tags` field | `remove_tags` field | Meaning |
|---|---|---|
| `would_add` | `would_remove` | Proposed AND on the item |
| `already_present` | `not_present` | Proposed but NOT on the item |
| `after_apply` | `after_apply` | What the tag list would look like after the operation |
| `added_count` | `removed_count` | Only on `status: applied` |

Common fields across both tools: `item_key`, `current_tags`, `dry_run`, `status`, `new_version` (only on applied).

### Error Handling

Every failure mode mirrors `apply_tags`. The implementation should detect the same conditions in the same places.

| # | Condition | Detected at | Response |
|---|---|---|---|
| 1 | Empty/whitespace tag list | input validation | `{"error": "No non-empty tags provided"}` |
| 2 | More than 25 tags requested | input validation | `{"error": "Refusing: N tags exceeds the per-call cap of 25..."}` |
| 3 | Item not in local SQLite (dry_run only) | `_local_snapshot` returns `None` | `{"error": "Reference '<key>' not found"}` |
| 4 | Missing creds (write only) | `api.get_item()` raises `MissingCredentialsError` | `{"error": "ZOTERO_USER_ID and ZOTERO_API_KEY must be set..."}` |
| 5 | Item not in Web API (write only) | `api.get_item()` returns 404 → `ZoteroApiError` | `{"error": "Item '<key>' not found in Zotero Web API"}` |
| 6 | All proposed tags `not_present` (write only) | post-diff short-circuit | `{"status": "no_changes", "would_remove": [], "not_present": [...]}` — no PATCH issued |
| 7 | Version conflict (HTTP 412) | `api.set_tags()` raises `VersionConflictError` | `{"error": "Item '<key>' was modified since version N...", "hint": "Re-run remove_tags to retry."}` |
| 8 | Network / 5xx / other HTTP error | `httpx` raises → `ZoteroApiError` | `{"error": "<HTTP error message>"}` |

### Asymmetry with `apply_tags`, called out explicitly

`apply_tags` is append-only: worst-case mistake is adding noise, recoverable via `remove_tags`. `remove_tags` removes intentional curation: worst-case mistake is harder to undo (the user has to remember the lost tag). Mitigations:

- `dry_run=True` by default — the user sees the diff before any write.
- Per-call cap of 25 tags — bounds blast radius.
- Docstring explicitly directs Claude to invoke this tool only when the user explicitly identifies tags to remove (no Claude-judgment removals in Phase A).

## Testing

New `TestRemoveTags` class in `tests/test_auto_tag.py`, parallel to `TestApplyTags`. **No new fixtures, no new test deps.** All existing helpers (`_FakeApi`, `_FakeZotero`, `_ref`, `api_config`, `mock_zotero_db`, `tmp_dir`) are reused.

| Test | What it pins |
|---|---|
| `test_rejects_empty_tags` | input validation |
| `test_rejects_too_many_tags` | cap enforcement |
| `test_dry_run_reads_local_and_skips_api` | uses `_FakeApi()` with no snapshot — `get_item` would AssertionError if called; passing test proves the API isn't touched in dry-run |
| `test_dry_run_works_without_credentials` | `_FakeApi(get_error=MissingCredentialsError)`; dry-run still produces a preview |
| `test_dry_run_returns_error_when_item_missing_locally` | empty `_FakeZotero()` → "not found" error |
| `test_remove_writes_diff` | dry_run=False; `set_tags` is called with `after_apply` (= current − proposed), correct version |
| `test_remove_skips_write_when_no_tags_match` | all proposed are `not_present` → status `no_changes`, `set_tags_calls == []` |
| `test_remove_idempotent_partial_match` | proposed = some-on-item + some-not-on-item → both `would_remove` and `not_present` populated, write happens for the matching subset |
| `test_remove_with_missing_credentials_returns_error` | dry_run=False + missing creds → error JSON |
| `test_remove_version_conflict_returns_hint` | HTTP 412 → error with retry hint |
| `test_remove_can_clear_all_tags` | removing every current tag → `after_apply: []`, status `applied` |

### What we deliberately don't re-test

- **HTTP layer (`api.set_tags` semantics)** — already covered by `TestZoteroApiClient.test_set_tags_sends_patch_with_version_header`. `remove_tags` uses the same primitive; we trust it once.
- **`ZoteroClient.get_reference` for the local read** — already covered in `test_zotero_client.py`. We reuse it via `_FakeZotero` and don't re-test the SQLite path here.

### Smoke Test

After the test suite is green, parallel to the script we used for `apply_tags` end-to-end, a `remove_tags` smoke test runs against the real Zotero library: dry-run preview → real remove → API re-fetch confirms the tag is gone server-side.

**Test count delta:** +11 tests (139 → 150).

## Deferred (documented for future sessions)

### Phase B — `suggest_tag_removals_context(item_key) -> str`

A read tool symmetrical to `suggest_tags_context`, oriented toward removal. Returns the abstract + current tags, with each current tag annotated with whether it appears semantically aligned with the abstract (Claude does the judging in chat after reading the response). Useful for proactive cleanup when the user suspects there are *more* mistakes than they have manually noticed — the bimodal nature of this user's library (CS/AI cluster + Sinology cluster) makes a Sinology paper accidentally tagged with AI tags hard to spot by eye.

**Why deferred:** the user's stated motivation is fixing already-spotted mistakes, not finding new ones. Phase A solves the urgent need.

### Other deferred items

- Library-wide tag rename (`nlp` → `NLP` everywhere). Different mental model from per-item operations; lives in a different spec.
- Collection editing (add/remove/move). Q2: E excluded this round; lives in a separate spec.

## Explicitly out of scope

| Excluded | Why |
|---|---|
| Fuzzy matching / "did you mean" suggestions | Would re-create the synonym-sprawl bug `vocabulary` anchoring was designed to prevent. Strict equality is safer. |
| Per-tag `i_understand=True` confirmation flags | `dry_run=True` is the safety gate; a 25-tag cap bounds blast radius. More flags = more surface, no net safety. |
| Bulk operations across multiple items in one call | Blast radius too wide for an LLM-driven flow. Per-item discipline preserves a clear undo unit. |
| Hard guard on "removing all tags" | Empty `after_apply` is a legitimate user intent (clear-and-restart); `dry_run` preview is the safety. No special-case. |

## Success Criteria

1. All 11 new tests pass; existing 139 still pass (150 total).
2. `ruff check src/ tests/` clean; format clean.
3. Smoke test against the real Zotero library: dry-run preview shows correct `would_remove` / `not_present` / `after_apply`; real remove (`dry_run=False`) returns `status: "applied"`; immediate API re-fetch confirms the removed tag is gone server-side.
4. `CLAUDE.md` updated to mention `remove_tags` alongside the existing two tools in the auto-tagging description.

## Files Touched

| File | Change |
|---|---|
| `src/mcp_local_reference/tools/auto_tag.py` | + `remove_tags` tool registration in `register_tools()`; + `remove_tags_impl()` module-level function |
| `tests/test_auto_tag.py` | + `TestRemoveTags` class with 11 tests; reuses existing helpers |
| `CLAUDE.md` | one-line note: `remove_tags` joins the auto-tagging tool family |

No changes to `services/zotero_api_client.py`, `services/zotero_client.py`, `config.py`, `pyproject.toml`, or any other module.
