# Tag Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third MCP tool, `remove_tags`, to the auto-tagging family so the user can correct tags that earlier auto-tagging or import workflows added incorrectly. Mirrors `apply_tags` structurally but in reverse.

**Architecture:** Sibling tool to `apply_tags` (Approach 1 from the spec). New `remove_tags` MCP wrapper in `register_tools()` calls a new module-level `remove_tags_impl()` function. Reuses the existing `ZoteroApiClient.set_tags` primitive (PATCH the full kept-tag list — no new HTTP method needed) and the existing `_local_snapshot` helper for the dry-run path. No changes to services, config, or pyproject.

**Tech Stack:** Python 3.11+, FastMCP, pydantic-settings, httpx (test layer uses `httpx.MockTransport`), pytest.

**Spec:** `docs/superpowers/specs/2026-04-27-tag-removal-design.md`

---

## Reference: Existing helpers being reused

The implementation reuses these (no changes needed). Engineers reading this out of order can find them already in the codebase:

- **`MAX_TAGS_PER_CALL = 25`** (module constant in `auto_tag.py`) — per-call cap.
- **`_local_snapshot(zotero, item_key) -> ItemSnapshot | None`** (module function in `auto_tag.py`) — reads current tags from local SQLite, returns `ItemSnapshot(item_key, version=0, tags=ref.tags, raw={})` or None if not found. The `version=0` is deliberately invalid for any write, so a local snapshot accidentally fed to `set_tags` would fail loudly with HTTP 412 instead of silently corrupting.
- **`ZoteroApiClient.get_item(item_key) -> ItemSnapshot`** — Web API GET. Raises `MissingCredentialsError` (no creds) or `ZoteroApiError` (404/HTTP error).
- **`ZoteroApiClient.set_tags(item_key, tags, version) -> int`** — Web API PATCH with `If-Unmodified-Since-Version`. Returns new version. Raises `VersionConflictError` (412) or `ZoteroApiError`.
- **`_FakeApi`** test helper — mimics `ZoteroApiClient` with `snapshot`, `get_error`, `set_error`, `new_version`, `set_tags_calls` fields. Calling `get_item()` with no `snapshot` set raises `AssertionError`.
- **`_FakeZotero`** test helper — mimics `ZoteroClient.get_reference`. Init: `_FakeZotero(references={"K": Reference(...)})`.
- **`_ref(item_key, tags)`** test helper — shorthand: returns `Reference(item_key=..., item_type="journalArticle", tags=...)`.
- **`api_config`** pytest fixture — returns a `Config` with valid test creds.

---

### Task 1: Add tool surface skeleton + input validation

Adds the MCP wrapper, the `remove_tags_impl` skeleton with input validation only, and the `TestRemoveTags` class with the two input-validation tests.

**Files:**
- Modify: `src/mcp_local_reference/tools/auto_tag.py`
- Modify: `tests/test_auto_tag.py`

- [ ] **Step 1: Write the two failing tests**

Add this class to `tests/test_auto_tag.py`, immediately after the existing `class TestApplyTags:` block (before `# === suggest_tags_context_impl ===`):

```python
# ======================================================================
# remove_tags_impl — orchestration via the same fakes
# ======================================================================


class TestRemoveTags:
    def test_rejects_empty_tags(self) -> None:
        result = json.loads(
            remove_tags_impl(_FakeApi(), _FakeZotero(), "K", ["", "  "], dry_run=True)  # type: ignore[arg-type]
        )
        assert "error" in result

    def test_rejects_too_many_tags(self) -> None:
        too_many = [f"t{i}" for i in range(MAX_TAGS_PER_CALL + 1)]
        result = json.loads(
            remove_tags_impl(_FakeApi(), _FakeZotero(), "K", too_many, dry_run=True)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "exceeds" in result["error"]
```

Also update the import at the top of `tests/test_auto_tag.py` to include `remove_tags_impl`:

```python
from mcp_local_reference.tools.auto_tag import (
    MAX_TAGS_PER_CALL,
    apply_tags_impl,
    remove_tags_impl,
    suggest_tags_context_impl,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags -v`
Expected: FAIL with `ImportError: cannot import name 'remove_tags_impl' from 'mcp_local_reference.tools.auto_tag'`

- [ ] **Step 3: Add the MCP wrapper in `register_tools`**

In `src/mcp_local_reference/tools/auto_tag.py`, inside `register_tools()`, immediately after the `apply_tags` `@mcp.tool()` block, add:

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

        Args:
            item_key: The 8-character Zotero item key.
            tags: Tags to remove. Empty strings and duplicates are ignored.
                Tags not currently on the item are silently skipped.
            dry_run: If True (default), report the diff without writing.
        """
        return remove_tags_impl(api, zotero, item_key, tags, dry_run)
```

- [ ] **Step 4: Add `remove_tags_impl` skeleton with input validation only**

In `src/mcp_local_reference/tools/auto_tag.py`, after `_build_and_maybe_write` (at the end of the file), add the new function:

```python
def remove_tags_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    item_key: str,
    tags: list[str],
    dry_run: bool,
) -> str:
    proposed = sorted({t.strip() for t in tags if t and t.strip()})
    if not proposed:
        return json.dumps({"error": "No non-empty tags provided"})
    if len(proposed) > MAX_TAGS_PER_CALL:
        return json.dumps(
            {
                "error": (
                    f"Refusing: {len(proposed)} tags exceeds the per-call cap of "
                    f"{MAX_TAGS_PER_CALL} (guards against runaway batch removal)."
                )
            }
        )
    # TODO(Task 2): dry-run path
    # TODO(Task 3): write path
    return json.dumps({"error": "remove_tags not fully implemented yet"})
```

(The two TODO placeholders are intentional — they are removed in Tasks 2 and 3 of this plan, not left in the final code.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags -v`
Expected: PASS — 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_local_reference/tools/auto_tag.py tests/test_auto_tag.py
git commit -m "feat(auto_tag): add remove_tags skeleton and input validation

Adds the remove_tags MCP wrapper and the remove_tags_impl skeleton
with input validation only (empty input + 25-tag cap). The dry-run
and write paths are stubbed and will be filled in by subsequent
commits. Two tests cover validation; suite passes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Dry-run path

Implements the dry-run branch using `_local_snapshot`, with three tests covering: API is not touched, missing-credentials does not block dry-run, and item-missing-locally returns an error.

**Files:**
- Modify: `src/mcp_local_reference/tools/auto_tag.py`
- Modify: `tests/test_auto_tag.py`

- [ ] **Step 1: Write the three failing tests**

Add these three test methods inside `class TestRemoveTags` (after the input-validation tests):

```python
    def test_dry_run_reads_local_and_skips_api(self) -> None:
        # _FakeApi() with no snapshot would AssertionError if get_item were called —
        # passing this test proves the dry-run path never hits the API.
        api = _FakeApi()
        zotero = _FakeZotero({"K": _ref("K", ["existing", "to-remove"])})
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["to-remove", "absent"], dry_run=True)  # type: ignore[arg-type]
        )
        assert result["status"] == "preview"
        assert result["would_remove"] == ["to-remove"]
        assert result["not_present"] == ["absent"]
        assert result["after_apply"] == ["existing"]
        assert api.set_tags_calls == []

    def test_dry_run_works_without_credentials(self) -> None:
        # If the credential check were on the dry-run path, this would fail.
        api = _FakeApi(get_error=MissingCredentialsError("set creds"))
        zotero = _FakeZotero({"K": _ref("K", ["x"])})
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["x"], dry_run=True)  # type: ignore[arg-type]
        )
        assert result["status"] == "preview"
        assert result["would_remove"] == ["x"]
        assert "error" not in result

    def test_dry_run_returns_error_when_item_missing_locally(self) -> None:
        api = _FakeApi()
        zotero = _FakeZotero()  # empty — no items
        result = json.loads(
            remove_tags_impl(api, zotero, "MISSING", ["x"], dry_run=True)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "MISSING" in result["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags -v`
Expected: 2 PASS (input validation from Task 1) + 3 FAIL with `KeyError: 'status'` or `AssertionError` because the skeleton returns `{"error": "remove_tags not fully implemented yet"}`.

- [ ] **Step 3: Replace the skeleton's TODO body with the dry-run path**

In `src/mcp_local_reference/tools/auto_tag.py`, replace the body of `remove_tags_impl` (everything *after* the input validation, i.e., the two TODO comments and the placeholder return) with the dry-run-only implementation:

```python
def remove_tags_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    item_key: str,
    tags: list[str],
    dry_run: bool,
) -> str:
    proposed = sorted({t.strip() for t in tags if t and t.strip()})
    if not proposed:
        return json.dumps({"error": "No non-empty tags provided"})
    if len(proposed) > MAX_TAGS_PER_CALL:
        return json.dumps(
            {
                "error": (
                    f"Refusing: {len(proposed)} tags exceeds the per-call cap of "
                    f"{MAX_TAGS_PER_CALL} (guards against runaway batch removal)."
                )
            }
        )

    if dry_run:
        snapshot = _local_snapshot(zotero, item_key)
        if snapshot is None:
            return json.dumps({"error": f"Reference '{item_key}' not found"})
    else:
        # TODO(Task 3): write path
        return json.dumps({"error": "remove_tags write path not implemented yet"})

    return _build_remove_plan(snapshot, item_key, proposed, dry_run, api=None)
```

Now add the `_build_remove_plan` helper at the end of the file (after `remove_tags_impl`):

```python
def _build_remove_plan(
    snapshot: Any,
    item_key: str,
    proposed: list[str],
    dry_run: bool,
    api: ZoteroApiClient | None,
) -> str:
    current = set(snapshot.tags)
    proposed_set = set(proposed)
    would_remove = sorted(proposed_set & current)
    not_present = sorted(proposed_set - current)
    after_apply = sorted(current - proposed_set)

    plan: dict[str, Any] = {
        "item_key": item_key,
        "current_tags": sorted(current),
        "would_remove": would_remove,
        "not_present": not_present,
        "after_apply": after_apply,
        "dry_run": dry_run,
    }

    if dry_run:
        plan["status"] = "preview"
        return json.dumps(plan, indent=2, ensure_ascii=False)
    # Task 3 fills in the write branch (uses api parameter).
    return json.dumps(plan, indent=2, ensure_ascii=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags -v`
Expected: 5 PASS (2 from Task 1 + 3 new dry-run tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/tools/auto_tag.py tests/test_auto_tag.py
git commit -m "feat(auto_tag): implement remove_tags dry-run path

Dry-run reads the snapshot from local SQLite via _local_snapshot —
no Web API credentials needed. Computes would_remove (proposed and
on the item), not_present (proposed but not on the item), and
after_apply (current minus proposed). Returns the preview JSON.

Three tests cover: API is not touched on dry-run; missing creds do
not block dry-run; item-missing-locally returns an error. Write
path is stubbed for the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Write path (core logic)

Implements the `dry_run=False` branch: GET the snapshot from the Web API for the authoritative version, compute the diff via the same `_build_remove_plan` helper, PATCH via `api.set_tags(item_key, after_apply, snapshot.version)`, and handle the three error categories (`MissingCredentialsError`, `ZoteroApiError`, `VersionConflictError`).

The single test driving this task is `test_remove_writes_diff`. Tests for partial-match, clear-all, missing-creds, and version-conflict are added in Task 4 (they exercise behavior introduced here) so this task remains tight.

**Files:**
- Modify: `src/mcp_local_reference/tools/auto_tag.py`
- Modify: `tests/test_auto_tag.py`

- [ ] **Step 1: Write the failing test**

Add this test method inside `class TestRemoveTags` (after the dry-run tests):

```python
    def test_remove_writes_diff(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["a", "b", "c"], raw={}),
            new_version=6,
        )
        zotero = _FakeZotero()  # not used when dry_run=False
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["b"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "applied"
        assert result["new_version"] == 6
        assert result["removed_count"] == 1
        # set_tags called with current minus removed, sorted, and the original version
        assert api.set_tags_calls == [("K", ["a", "c"], 5)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags::test_remove_writes_diff -v`
Expected: FAIL with `KeyError: 'status'` or `AssertionError` because the write branch still returns the placeholder error JSON from Task 2.

- [ ] **Step 3: Implement the write branch**

In `src/mcp_local_reference/tools/auto_tag.py`, replace the body of `remove_tags_impl` (replacing the Task-2 stubbed `else` branch and the placeholder write-path return at the bottom). The full function should now read:

```python
def remove_tags_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    item_key: str,
    tags: list[str],
    dry_run: bool,
) -> str:
    proposed = sorted({t.strip() for t in tags if t and t.strip()})
    if not proposed:
        return json.dumps({"error": "No non-empty tags provided"})
    if len(proposed) > MAX_TAGS_PER_CALL:
        return json.dumps(
            {
                "error": (
                    f"Refusing: {len(proposed)} tags exceeds the per-call cap of "
                    f"{MAX_TAGS_PER_CALL} (guards against runaway batch removal)."
                )
            }
        )

    if dry_run:
        snapshot = _local_snapshot(zotero, item_key)
        if snapshot is None:
            return json.dumps({"error": f"Reference '{item_key}' not found"})
    else:
        try:
            snapshot = api.get_item(item_key)
        except MissingCredentialsError as e:
            return json.dumps({"error": str(e)})
        except ZoteroApiError as e:
            return json.dumps({"error": str(e)})

    return _build_remove_plan(snapshot, item_key, proposed, dry_run, api)
```

Now update `_build_remove_plan` to call `api.set_tags` on the write path. Replace the entire function with:

```python
def _build_remove_plan(
    snapshot: Any,
    item_key: str,
    proposed: list[str],
    dry_run: bool,
    api: ZoteroApiClient | None,
) -> str:
    current = set(snapshot.tags)
    proposed_set = set(proposed)
    would_remove = sorted(proposed_set & current)
    not_present = sorted(proposed_set - current)
    after_apply = sorted(current - proposed_set)

    plan: dict[str, Any] = {
        "item_key": item_key,
        "current_tags": sorted(current),
        "would_remove": would_remove,
        "not_present": not_present,
        "after_apply": after_apply,
        "dry_run": dry_run,
    }

    if dry_run:
        plan["status"] = "preview"
        return json.dumps(plan, indent=2, ensure_ascii=False)

    # Task 4 will add the no-changes short-circuit here.
    assert api is not None  # write path always passes a real api
    try:
        new_version = api.set_tags(item_key, after_apply, snapshot.version)
    except VersionConflictError as e:
        return json.dumps({"error": str(e), "hint": "Re-run remove_tags to retry."})
    except ZoteroApiError as e:
        return json.dumps({"error": str(e)})

    plan["status"] = "applied"
    plan["new_version"] = new_version
    plan["removed_count"] = len(would_remove)
    return json.dumps(plan, indent=2, ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags::test_remove_writes_diff -v`
Expected: PASS.

Also re-run the full `TestRemoveTags` to confirm nothing regressed:

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/tools/auto_tag.py tests/test_auto_tag.py
git commit -m "feat(auto_tag): implement remove_tags write path

Write path (dry_run=False) fetches the authoritative snapshot from
the Web API for the version number, computes the same diff as the
dry-run path, then PATCHes via api.set_tags(item_key, after_apply,
version). Error handling covers MissingCredentialsError,
ZoteroApiError, and VersionConflictError, all with the same response
shape as apply_tags. The no-changes short-circuit is added next.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Write path edge cases — no_changes short-circuit, partial match, clear-all, missing creds, version conflict

Adds five tests covering write-path edge cases. Most pass against the Task 3 implementation; the no-changes short-circuit requires a small impl change driven by `test_remove_skips_write_when_no_tags_match`.

**Files:**
- Modify: `src/mcp_local_reference/tools/auto_tag.py`
- Modify: `tests/test_auto_tag.py`

- [ ] **Step 1: Write the no-changes failing test**

Add this test inside `class TestRemoveTags` (after `test_remove_writes_diff`):

```python
    def test_remove_skips_write_when_no_tags_match(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["a", "b"], raw={})
        )
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["x", "y"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "no_changes"
        assert result["would_remove"] == []
        assert sorted(result["not_present"]) == ["x", "y"]
        assert api.set_tags_calls == []  # no PATCH issued
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags::test_remove_skips_write_when_no_tags_match -v`
Expected: FAIL — `result["status"]` is `"applied"` (Task 3's impl always calls `set_tags` even when `would_remove` is empty), and `api.set_tags_calls` is non-empty.

- [ ] **Step 3: Add the no-changes short-circuit**

In `src/mcp_local_reference/tools/auto_tag.py`, modify `_build_remove_plan` to short-circuit before the write call when `would_remove` is empty. Insert the short-circuit immediately after the `dry_run` branch in `_build_remove_plan` and before the `assert api is not None`:

```python
    if dry_run:
        plan["status"] = "preview"
        return json.dumps(plan, indent=2, ensure_ascii=False)
    if not would_remove:
        plan["status"] = "no_changes"
        return json.dumps(plan, indent=2, ensure_ascii=False)

    assert api is not None
    try:
        new_version = api.set_tags(item_key, after_apply, snapshot.version)
    ...
```

(The rest of `_build_remove_plan` is unchanged from Task 3.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags::test_remove_skips_write_when_no_tags_match -v`
Expected: PASS.

- [ ] **Step 5: Add four more edge-case tests (all should pass against the existing impl)**

Add these four tests inside `class TestRemoveTags`:

```python
    def test_remove_idempotent_partial_match(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["a", "b"], raw={}),
            new_version=6,
        )
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["b", "absent"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "applied"
        assert result["would_remove"] == ["b"]
        assert result["not_present"] == ["absent"]
        assert result["after_apply"] == ["a"]
        assert api.set_tags_calls == [("K", ["a"], 5)]

    def test_remove_can_clear_all_tags(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["a", "b"], raw={}),
            new_version=6,
        )
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["a", "b"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "applied"
        assert result["after_apply"] == []
        assert api.set_tags_calls == [("K", [], 5)]

    def test_remove_with_missing_credentials_returns_error(self) -> None:
        api = _FakeApi(get_error=MissingCredentialsError("set creds"))
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["x"], dry_run=False)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "creds" in result["error"]

    def test_remove_version_conflict_returns_hint(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["x"], raw={}),
            set_error=VersionConflictError("conflict"),
        )
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["x"], dry_run=False)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "hint" in result
```

- [ ] **Step 6: Run all `TestRemoveTags` tests to verify the full suite passes**

Run: `uv run pytest tests/test_auto_tag.py::TestRemoveTags -v`
Expected: 11 PASS (all `TestRemoveTags` tests).

Also run the full suite to confirm no regressions elsewhere:

Run: `uv run pytest -q`
Expected: 150 passed (was 139 before this work).

- [ ] **Step 7: Commit**

```bash
git add src/mcp_local_reference/tools/auto_tag.py tests/test_auto_tag.py
git commit -m "feat(auto_tag): write-path edge cases for remove_tags

Adds the no-changes short-circuit (would_remove empty → no PATCH
issued) and four edge-case tests covering: partial match (some
tags on item, others absent), clear-all (after_apply becomes empty
list), missing credentials, and version conflict. All eleven tests
in TestRemoveTags now pass; full suite at 150 (was 139).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Update CLAUDE.md

One-line addition to the auto-tagging architecture description so the doc reflects all three tools, not just two.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the auto-tagging description**

In `CLAUDE.md`, find the bullet that begins:

```
- **Auto-tagging (`tools/auto_tag.py`):** two MCP tools designed for human-in-the-loop tagging — `suggest_tags_context` (local read; ...) and `apply_tags` (Web API write; ...).
```

Replace `two MCP tools` with `three MCP tools` and append `, plus `remove_tags` (Web API write; mirror of apply_tags but for removal — append-only's inverse, idempotent on tags not present, dry-run reads from local SQLite without creds)` to the end of that bullet (before the next bullet starts).

The full updated bullet should read:

```
- **Auto-tagging (`tools/auto_tag.py`):** three MCP tools designed for human-in-the-loop tagging — `suggest_tags_context` (local read; returns title + abstract + current tags + top-30 vocabulary), `apply_tags` (Web API write; append-only set union, `dry_run=True` by default with cap of 25 tags, optimistic concurrency via `If-Unmodified-Since-Version`), and `remove_tags` (Web API write; mirror of `apply_tags` for removal — same dry-run + cap + concurrency semantics, idempotent on tags not present). Dry-run for both write tools reads from local SQLite (no creds needed); real writes require `ZOTERO_USER_ID` + `ZOTERO_API_KEY`
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: mention remove_tags in CLAUDE.md auto-tagging description

The auto-tagging family now has three tools, not two. Note that
remove_tags is a structural mirror of apply_tags (same dry-run,
same cap, same concurrency control), idempotent on tags not on
the item.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Verification — full suite, lint, server smoke

Confirms the green-bar conditions from the spec's Success Criteria. No new code; only verification commands.

**Files:** none modified.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: `150 passed` (139 existing + 11 new).

If any test fails: investigate, fix the issue in the appropriate task above, re-run. Do not proceed until all 150 pass.

- [ ] **Step 2: Run ruff check + format**

Run: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
Expected: `All checks passed!` and `36 files already formatted` (or similar — the count may differ if other files have changed).

If lint complains: run `uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/`, then re-verify with the previous command.

- [ ] **Step 3: Server smoke test — confirm `remove_tags` is registered**

Run:

```bash
.venv/bin/python -c "
from mcp_local_reference.server import create_server
mcp = create_server()
import asyncio
tools = {t.name for t in asyncio.run(mcp.list_tools())}
assert 'remove_tags' in tools, f'remove_tags missing; tools: {sorted(tools)}'
assert 'apply_tags' in tools and 'suggest_tags_context' in tools
print('OK: server builds, all three auto-tagging tools registered')
print('Total tools:', len(tools))
"
```

Expected output:
```
OK: server builds, all three auto-tagging tools registered
Total tools: 15
```

(15 = the 14 tools registered before this work, plus `remove_tags`.)

- [ ] **Step 4: Commit any final fixes (if Steps 1 or 2 required edits)**

If no fixes were needed in Steps 1–3, skip this step. Otherwise:

```bash
git add -p   # interactively stage only the lint/format fixes
git commit -m "chore: lint and format fixes for remove_tags work

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: End-to-end smoke against the real Zotero library (operator-driven)**

This is a manual validation step, not an automated test. The user runs it after restarting Claude Desktop (which re-spawns the MCP server with the new `remove_tags` tool registered).

The user picks an item with a tag they want to remove and walks through:

1. Call `suggest_tags_context(item_key)` to confirm the tool inventory shows three tools.
2. Call `remove_tags(item_key, [bad_tag], dry_run=True)` — verify the preview shows correct `would_remove` / `not_present` / `after_apply`.
3. Call `remove_tags(item_key, [bad_tag], dry_run=False)` — verify status `applied`, a new `new_version`, and `removed_count: 1`.
4. Re-fetch via the Web API (or just re-call `remove_tags` with the same arg) — the second call should return `status: "no_changes"` because the tag is now gone.

A passing manual smoke means Phase A is fully shipped.

---

## Self-Review

**1. Spec coverage** — every requirement in the spec maps to a task above:

- Tool surface (Section: Tool Surface) → Task 1 (skeleton) + Task 3 (write branch wired up).
- Composition with existing tools (Section: Composition) → Task 5 (CLAUDE.md update mentioning the three tools together).
- Dry-run data flow (Section: Data Flow — Dry-run path) → Task 2.
- Write data flow (Section: Data Flow — Write path) → Task 3 (basic) + Task 4 (no-changes short-circuit).
- Response payload (Section: Response Payload) → Task 2 (dry-run shape) + Tasks 3–4 (write shape).
- Error handling rows 1–8 (Section: Error Handling) → row 1+2 in Task 1; row 3 in Task 2; rows 4, 5, 7, 8 in Task 3; row 6 in Task 4.
- Asymmetry mitigations (dry-run default, 25-cap, docstring discipline) → Task 1 (defaults + cap + docstring all included).
- Testing — 11 tests (Section: Testing) → Tasks 1–4 add all 11 tests; Task 6 verifies test count is 150.
- Smoke test (Section: Smoke Test) → Task 6 Step 5.
- Files Touched (Section: Files Touched) → 3 files: auto_tag.py (Tasks 1–4), test_auto_tag.py (Tasks 1–4), CLAUDE.md (Task 5). Verified no other files modified.

No gaps. No tasks for items the spec doesn't require.

**2. Placeholder scan** — searched the plan for spec failures:

- "TBD" / "TODO" / "implement later" — only TWO `# TODO(Task N):` comments appear in transient code (Task 1 step 4 and Task 2 step 3) and are explicitly stated as transient: each is removed in the named follow-up task. No placeholders survive past Task 4.
- "Add appropriate error handling" / vague statements — none. Every error path has a specific exception type and a specific JSON response shape.
- Code without tests — none. Every behavior change has a corresponding test in the same or earlier task.
- "Similar to Task N" — none. Each test method has its full code shown even when conceptually parallel to one in `TestApplyTags`.

**3. Type / signature consistency** — checked the names used across tasks:

- `remove_tags_impl(api, zotero, item_key, tags, dry_run)` — same five-argument signature in Task 1 (skeleton), Task 2 (dry-run impl), Task 3 (full impl), and the test calls in Tasks 1–4. ✓
- `_build_remove_plan(snapshot, item_key, proposed, dry_run, api)` — introduced in Task 2 with `api: ZoteroApiClient | None`, signature unchanged in Task 3 (assertion narrows api to non-None on the write branch). ✓
- Response field names — `would_remove`, `not_present`, `after_apply`, `current_tags`, `removed_count`, `new_version`, `status`, `dry_run` — used consistently across tests and impl. ✓
- Test helper signatures — `_FakeApi(...)`, `_FakeZotero({...})`, `_ref(item_key, tags)`, `ItemSnapshot(item_key, version, tags, raw)` — match the existing helpers; no new fixtures needed.

No inconsistencies. Plan is self-consistent and complete.
