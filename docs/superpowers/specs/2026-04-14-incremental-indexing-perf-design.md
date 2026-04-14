# Incremental Indexing Performance Design

**Date:** 2026-04-14
**Goal:** Make `code-mcp-index` incremental re-indexing fast when most files are unchanged.

## Problem

Incremental re-indexing is slow because:
1. Every file triggers a new SQLite connection (`_connect()` creates fresh connections)
2. Every file's content is read and SHA256-hashed even when unchanged
3. Phase 2 (vector embedding) re-embeds ALL symbols, not just changed ones

## Design

Two complementary optimizations: **A** (connection reuse + bulk hash check) and **C** (incremental vector embedding).

---

### A. Connection Reuse + Bulk Hash Check

#### A1. Persistent SQLite Connection

`CodeFTSIndex` currently creates a new `sqlite3.Connection` on every `_connect()` call. Change to a single persistent connection stored as `self._conn`, reused across all operations within a session.

**File:** `src/code_mcp/code_fts.py`

- Add `self._conn` in `__init__`, initialized once
- `_connect()` returns `self._conn` instead of creating new
- Add `close()` method for cleanup
- Existing `with self._connect() as conn:` blocks continue to work (SQLite context manager commits/rollbacks without closing on a persistent connection)

#### A2. mtime Column

Add `file_mtime REAL` column to `code_files` table. Migration uses the existing `ALTER TABLE ADD COLUMN` + `contextlib.suppress(OperationalError)` pattern.

**File:** `src/code_mcp/code_fts.py`

- Add column in `_init_db()`
- Store mtime in `upsert_file()` and `add_files_batch()`

#### A3. Bulk Hash Lookup

New method `get_all_file_hashes(repo_id) -> dict[str, tuple[str, float | None]]` returns `{rel_path: (file_hash, mtime)}` for all files in a repo, in a single query.

**File:** `src/code_mcp/code_fts.py`

#### A4. mtime-First Skip Logic in index_repo()

Replace per-file `needs_indexing()` DB calls with in-memory checks:

```
1. stat() the file -> get current mtime
2. Look up (stored_hash, stored_mtime) from bulk dict
3. If stored_mtime == current_mtime -> skip (no read needed)
4. If mtime differs -> compute SHA256 -> compare hash
5. If hash matches -> update stored mtime only, skip
6. If hash differs -> re-index the file
```

**File:** `src/code_mcp/code_manager.py`

- Call `get_all_file_hashes()` once at start of `index_repo()`
- Replace `_parse_file_for_index()`'s internal `needs_indexing()` call with the mtime-first check
- Batch-update mtimes for files that had hash match but stale mtime (new method `update_mtimes_batch(repo_id, updates: list[tuple[str, float]])` — single UPDATE query)

---

### C. Incremental Vector Embedding

#### C1. Selective Delete + Add

Replace `create_table(data, mode="overwrite")` in `add_symbols_batch()` with:

1. If table doesn't exist -> `create_table(data)`
2. If table exists:
   - Collect changed file_ids from the batch
   - `table.delete(f"file_id IN ({id_list}}")` — remove old embeddings for changed files
   - `table.add(new_data)` — insert new embeddings

Unchanged files' embeddings are preserved.

**File:** `src/code_mcp/code_embedder.py`

#### C2. Compaction

Add `compact()` method that calls `table.compact_files()` to defragment LanceDB after many incremental updates.

**File:** `src/code_mcp/code_embedder.py`

Expose via `--compact` CLI flag.

**File:** `src/code_mcp/cli.py`

---

## Files Changed

| File | Changes |
|------|---------|
| `code_fts.py` | Persistent connection, `close()`, `get_all_file_hashes()`, `update_mtimes_batch()`, `file_mtime` column, store mtime in upserts |
| `code_manager.py` | Bulk hash+mtime lookup, mtime-first skip logic, pass only changed file_ids to Phase 2 |
| `code_embedder.py` | Selective delete+add in `add_symbols_batch()`, `compact()` method |
| `cli.py` | `--compact` flag |

## Files NOT Changed

| File | Reason |
|------|--------|
| `models.py` | No model changes needed |
| `config.py` | No new settings needed |
| `parser.py` | Parsing logic unchanged |
| `server.py` | Search/MCP interface unchanged |
| `code_watcher.py` | Already uses `reindex_file()`, benefits from persistent connection |

## Migration

- `file_mtime` column: `ALTER TABLE code_files ADD COLUMN file_mtime REAL` with `contextlib.suppress(OperationalError)`. Existing rows get `NULL`, falling through to hash check on first run.
- No breaking changes to CLI or MCP interface.

## Expected Impact

- Incremental re-index of 10,000 unchanged files: ~30s+ -> ~2-3s (mtime stat check only, no file reads, no DB roundtrips)
- Incremental vector embedding of 50 changed files: embeds ~50 files instead of all ~50,000 symbols
