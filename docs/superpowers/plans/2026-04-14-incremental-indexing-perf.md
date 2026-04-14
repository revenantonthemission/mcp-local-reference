# Incremental Indexing Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `code-mcp-index` incremental re-indexing fast when most files are unchanged, by adding persistent SQLite connections, mtime-based skip logic, and incremental vector embedding.

**Architecture:** Two independent optimizations layered onto the existing 2-phase indexing pipeline. Optimization A (Tasks 1-4) targets Phase 1 by eliminating per-file DB connections and file reads via mtime checks. Optimization C (Tasks 5-6) targets Phase 2 by replacing full-table vector overwrites with selective delete+add. Each optimization is independently valuable and testable.

**Tech Stack:** Python 3.11+, SQLite FTS5, LanceDB, sentence-transformers, pytest

---

## File Structure

| File | Role | Change Type |
|------|------|-------------|
| `src/code_mcp/code_fts.py` | SQLite FTS5 index | Modify: persistent connection, mtime column, bulk hash lookup, mtime batch update |
| `src/code_mcp/code_manager.py` | Indexing facade | Modify: mtime-first skip logic in `index_repo()` |
| `src/code_mcp/code_embedder.py` | Vector embedding | Modify: selective delete+add, compact method |
| `src/code_mcp/cli.py` | CLI entry point | Modify: add `--compact` flag |
| `tests/code_mcp/test_code_fts.py` | FTS tests | Modify: add tests for new methods |
| `tests/code_mcp/test_code_manager.py` | Manager tests | Modify: add mtime skip tests |
| `tests/code_mcp/test_code_embedder.py` | Embedder tests | Create: tests for incremental embedding |

---

### Task 1: Persistent SQLite Connection in CodeFTSIndex

**Files:**
- Modify: `src/code_mcp/code_fts.py:33-44` (constructor and `_connect`)
- Test: `tests/code_mcp/test_code_fts.py`

- [ ] **Step 1: Write failing test for persistent connection**

Add to `tests/code_mcp/test_code_fts.py`:

```python
class TestPersistentConnection:
    def test_connect_returns_same_connection(self, fts_index: CodeFTSIndex) -> None:
        conn1 = fts_index._connect()
        conn2 = fts_index._connect()
        assert conn1 is conn2

    def test_close_closes_connection(self, fts_index: CodeFTSIndex) -> None:
        fts_index.close()
        # After close, a new connection should be created
        conn = fts_index._connect()
        # Should work fine — new connection created
        row = conn.execute("SELECT 1").fetchone()
        assert row[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/code_mcp/test_code_fts.py::TestPersistentConnection -v`
Expected: FAIL — `_connect()` creates new connections each time, `close()` doesn't exist.

- [ ] **Step 3: Implement persistent connection**

In `src/code_mcp/code_fts.py`, modify `CodeFTSIndex`:

```python
class CodeFTSIndex:
    """SQLite FTS5 full-text index for source code symbols."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or settings.index_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        """Close the persistent connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/code_mcp/test_code_fts.py::TestPersistentConnection -v`
Expected: PASS

- [ ] **Step 5: Run full FTS test suite to verify no regressions**

Run: `pytest tests/code_mcp/test_code_fts.py -v`
Expected: All tests PASS. Existing `with self._connect() as conn:` blocks still work because SQLite's context manager on a persistent connection commits/rollbacks without closing.

- [ ] **Step 6: Commit**

```bash
git add src/code_mcp/code_fts.py tests/code_mcp/test_code_fts.py
git commit -m "perf: persistent SQLite connection in CodeFTSIndex"
```

---

### Task 2: Add file_mtime Column and Bulk Hash Lookup

**Files:**
- Modify: `src/code_mcp/code_fts.py:46-99` (`_init_db`), `src/code_mcp/code_fts.py:136-169` (`upsert_file`), `src/code_mcp/code_fts.py:197-310` (`add_files_batch`)
- Test: `tests/code_mcp/test_code_fts.py`

- [ ] **Step 1: Write failing tests for mtime column and bulk hash lookup**

Add to `tests/code_mcp/test_code_fts.py`:

```python
class TestMtimeAndBulkHash:
    def test_upsert_file_stores_mtime(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "main.py", "python", 1024, "abc123", file_mtime=1000.5)

        hashes = fts_index.get_all_file_hashes(repo_id)
        assert "main.py" in hashes
        assert hashes["main.py"] == ("abc123", 1000.5)

    def test_upsert_file_mtime_defaults_to_none(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "main.py", "python", 1024, "abc123")

        hashes = fts_index.get_all_file_hashes(repo_id)
        assert hashes["main.py"] == ("abc123", None)

    def test_get_all_file_hashes_returns_all_files(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "a.py", "python", 100, "h1", file_mtime=1.0)
        fts_index.upsert_file(repo_id, "b.py", "python", 200, "h2", file_mtime=2.0)

        hashes = fts_index.get_all_file_hashes(repo_id)
        assert len(hashes) == 2
        assert hashes["a.py"] == ("h1", 1.0)
        assert hashes["b.py"] == ("h2", 2.0)

    def test_get_all_file_hashes_empty_repo(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        hashes = fts_index.get_all_file_hashes(repo_id)
        assert hashes == {}

    def test_get_all_file_hashes_ignores_other_repos(self, fts_index: CodeFTSIndex) -> None:
        r1 = fts_index.upsert_repo("repo1", Path("/tmp/repo1"))
        r2 = fts_index.upsert_repo("repo2", Path("/tmp/repo2"))
        fts_index.upsert_file(r1, "a.py", "python", 100, "h1", file_mtime=1.0)
        fts_index.upsert_file(r2, "b.py", "python", 200, "h2", file_mtime=2.0)

        hashes = fts_index.get_all_file_hashes(r1)
        assert len(hashes) == 1
        assert "a.py" in hashes

    def test_update_mtimes_batch(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "a.py", "python", 100, "h1", file_mtime=1.0)
        fts_index.upsert_file(repo_id, "b.py", "python", 200, "h2", file_mtime=2.0)

        fts_index.update_mtimes_batch(repo_id, [("a.py", 10.0), ("b.py", 20.0)])

        hashes = fts_index.get_all_file_hashes(repo_id)
        assert hashes["a.py"] == ("h1", 10.0)
        assert hashes["b.py"] == ("h2", 20.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/code_mcp/test_code_fts.py::TestMtimeAndBulkHash -v`
Expected: FAIL — `upsert_file` doesn't accept `file_mtime`, `get_all_file_hashes` and `update_mtimes_batch` don't exist.

- [ ] **Step 3: Implement mtime column migration**

In `src/code_mcp/code_fts.py`, in `_init_db()`, add after the existing `parent_name` migration:

```python
with contextlib.suppress(sqlite3.OperationalError):
    conn.execute("ALTER TABLE code_files ADD COLUMN file_mtime REAL")
```

- [ ] **Step 4: Update upsert_file to accept and store file_mtime**

In `src/code_mcp/code_fts.py`, modify `upsert_file`:

```python
def upsert_file(
    self,
    repo_id: int,
    rel_path: str,
    language: str,
    size_bytes: int,
    file_hash: str,
    file_mtime: float | None = None,
) -> int:
    """Insert or update a file record, return file_id."""
    with self._connect() as conn:
        # Remove old symbols if file exists
        existing = conn.execute(
            "SELECT file_id FROM code_files WHERE repo_id = ? AND rel_path = ?",
            (repo_id, rel_path),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM code_symbols WHERE file_id = ?",
                (existing["file_id"],),
            )

        conn.execute(
            "INSERT INTO code_files (repo_id, rel_path, language, size_bytes, file_hash, file_mtime) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(repo_id, rel_path) DO UPDATE SET "
            "language=excluded.language, size_bytes=excluded.size_bytes, "
            "file_hash=excluded.file_hash, file_mtime=excluded.file_mtime, "
            "indexed_at=datetime('now')",
            (repo_id, rel_path, language, size_bytes, file_hash, file_mtime),
        )
        row = conn.execute(
            "SELECT file_id FROM code_files WHERE repo_id = ? AND rel_path = ?",
            (repo_id, rel_path),
        ).fetchone()
        return row["file_id"]
```

- [ ] **Step 5: Implement get_all_file_hashes**

Add to `CodeFTSIndex`:

```python
def get_all_file_hashes(self, repo_id: int) -> dict[str, tuple[str, float | None]]:
    """Get all (rel_path -> (file_hash, file_mtime)) for a repo in one query."""
    with self._connect() as conn:
        rows = conn.execute(
            "SELECT rel_path, file_hash, file_mtime FROM code_files WHERE repo_id = ?",
            (repo_id,),
        ).fetchall()
        return {
            row["rel_path"]: (row["file_hash"], row["file_mtime"])
            for row in rows
        }
```

- [ ] **Step 6: Implement update_mtimes_batch**

Add to `CodeFTSIndex`:

```python
def update_mtimes_batch(self, repo_id: int, updates: list[tuple[str, float]]) -> None:
    """Batch-update file_mtime for files whose hash matched but mtime was stale."""
    if not updates:
        return
    with self._connect() as conn:
        conn.executemany(
            "UPDATE code_files SET file_mtime = ? WHERE repo_id = ? AND rel_path = ?",
            [(mtime, repo_id, rel_path) for rel_path, mtime in updates],
        )
```

- [ ] **Step 7: Update add_files_batch to store file_mtime**

In `src/code_mcp/code_fts.py`, modify `add_files_batch`:

Change the `files_data` type hint from `list[tuple[str, str, int, str, list]]` to `list[tuple[str, str, int, str, float | None, list]]` — adding `file_mtime` between `file_hash` and `symbols`.

Update the loop unpacking and INSERT:

```python
def add_files_batch(
    self,
    repo_id: int,
    files_data: list[tuple[str, str, int, str, float | None, list]],
) -> list[tuple[int, list]]:
    """Batch insert files and symbols in a single transaction.

    Disables FTS triggers during bulk insert and rebuilds FTS index
    once at the end, which is much faster than per-row trigger updates.

    Args:
        repo_id: Repository ID.
        files_data: List of (rel_path, language, size_bytes, file_hash, file_mtime, symbols).

    Returns:
        List of (file_id, symbols) tuples for downstream use (e.g. embedding).
    """
    if not files_data:
        return []

    results: list[tuple[int, list]] = []
    with self._connect() as conn:
        # Suspend FTS triggers — rebuild index once at the end
        conn.execute("DROP TRIGGER IF EXISTS code_symbols_ai")
        conn.execute("DROP TRIGGER IF EXISTS code_symbols_ad")
        conn.execute("DROP TRIGGER IF EXISTS code_symbols_au")

        # Upsert files and collect file_ids
        file_id_map: dict[str, int] = {}
        for rel_path, language, size_bytes, file_hash, file_mtime, symbols in files_data:
            existing = conn.execute(
                "SELECT file_id FROM code_files WHERE repo_id = ? AND rel_path = ?",
                (repo_id, rel_path),
            ).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM code_symbols WHERE file_id = ?",
                    (existing["file_id"],),
                )

            conn.execute(
                "INSERT INTO code_files "
                "(repo_id, rel_path, language, size_bytes, file_hash, file_mtime) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repo_id, rel_path) DO UPDATE SET "
                "language=excluded.language, size_bytes=excluded.size_bytes, "
                "file_hash=excluded.file_hash, file_mtime=excluded.file_mtime, "
                "indexed_at=datetime('now')",
                (repo_id, rel_path, language, size_bytes, file_hash, file_mtime),
            )
            row = conn.execute(
                "SELECT file_id FROM code_files WHERE repo_id = ? AND rel_path = ?",
                (repo_id, rel_path),
            ).fetchone()
            file_id_map[rel_path] = row["file_id"]

        # Bulk insert ALL symbols in one executemany call
        all_symbol_rows: list[tuple] = []
        for rel_path, language, size_bytes, file_hash, file_mtime, symbols in files_data:
            file_id = file_id_map[rel_path]
            for s in symbols:
                s.file_id = file_id
                all_symbol_rows.append((
                    file_id, s.name, s.symbol_type, s.signature,
                    s.text, s.start_line, s.end_line, s.language, s.parent_name,
                ))
            results.append((file_id, symbols))

        if all_symbol_rows:
            # Track first new symbol_id for selective FTS insert
            row = conn.execute(
                "SELECT COALESCE(MAX(symbol_id), 0) FROM code_symbols"
            ).fetchone()
            first_new_id = row[0] + 1

            conn.executemany(
                "INSERT INTO code_symbols "
                "(file_id, symbol_name, symbol_type, signature, text, "
                "start_line, end_line, language, parent_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                all_symbol_rows,
            )

            # Insert FTS entries only for newly added symbols
            conn.execute(
                "INSERT INTO code_symbols_fts(rowid, symbol_name, text) "
                "SELECT symbol_id, symbol_name, text FROM code_symbols "
                "WHERE symbol_id >= ?",
                (first_new_id,),
            )

        # Restore triggers
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS code_symbols_ai AFTER INSERT ON code_symbols
            BEGIN
                INSERT INTO code_symbols_fts(rowid, symbol_name, text)
                VALUES (new.symbol_id, new.symbol_name, new.text);
            END;

            CREATE TRIGGER IF NOT EXISTS code_symbols_ad AFTER DELETE ON code_symbols
            BEGIN
                INSERT INTO code_symbols_fts(code_symbols_fts, rowid, symbol_name, text)
                VALUES ('delete', old.symbol_id, old.symbol_name, old.text);
            END;

            CREATE TRIGGER IF NOT EXISTS code_symbols_au AFTER UPDATE ON code_symbols
            BEGIN
                INSERT INTO code_symbols_fts(code_symbols_fts, rowid, symbol_name, text)
                VALUES ('delete', old.symbol_id, old.symbol_name, old.text);
                INSERT INTO code_symbols_fts(rowid, symbol_name, text)
                VALUES (new.symbol_id, new.symbol_name, new.text);
            END;
        """)

    return results
```

- [ ] **Step 8: Run tests**

Run: `pytest tests/code_mcp/test_code_fts.py -v`
Expected: All tests PASS (including existing batch tests — update their call sites if needed).

Note: Existing `add_files_batch` callers in `code_manager.py` need to be updated in Task 3 to pass the `file_mtime` field. If the FTS batch tests call `add_files_batch` directly, update them now to pass `None` as the mtime:

In `tests/code_mcp/test_code_fts.py`, update `TestBatchInsert` fixtures — change tuples from `(rel_path, lang, size, hash, symbols)` to `(rel_path, lang, size, hash, None, symbols)`.

- [ ] **Step 9: Commit**

```bash
git add src/code_mcp/code_fts.py tests/code_mcp/test_code_fts.py
git commit -m "perf: add file_mtime column, bulk hash lookup, and mtime batch update"
```

---

### Task 3: mtime-First Skip Logic in CodeIndexManager

**Files:**
- Modify: `src/code_mcp/code_manager.py:49-158` (`index_repo` and `_parse_file_for_index`)
- Test: `tests/code_mcp/test_code_manager.py`

- [ ] **Step 1: Write failing tests for mtime-based skipping**

Add to `tests/code_mcp/test_code_manager.py`:

```python
import time


class TestMtimeSkipping:
    def test_second_run_uses_mtime_to_skip(
        self, manager: CodeIndexManager, sample_repo: Path
    ) -> None:
        """Second indexing run should skip unchanged files via mtime (no file reads)."""
        stats1 = manager.index_repo(sample_repo, skip_vectors=True)
        assert stats1["indexed"] > 0

        stats2 = manager.index_repo(sample_repo, skip_vectors=True)
        # All files skipped on second run
        assert stats2["skipped"] == stats1["total"]
        assert stats2["indexed"] == 0

    def test_modified_file_gets_reindexed(
        self, manager: CodeIndexManager, sample_repo: Path
    ) -> None:
        """A file with changed mtime and content should be re-indexed."""
        manager.index_repo(sample_repo, skip_vectors=True)

        # Modify a file (changes both mtime and hash)
        target = sample_repo / "main.py"
        time.sleep(0.05)  # ensure mtime changes
        target.write_text("def new_func():\n    return 'changed'\n")

        stats = manager.index_repo(sample_repo, skip_vectors=True)
        assert stats["indexed"] >= 1

    def test_touched_file_with_same_content_skipped(
        self, manager: CodeIndexManager, sample_repo: Path
    ) -> None:
        """A file whose mtime changed but content is identical should be skipped."""
        manager.index_repo(sample_repo, skip_vectors=True)

        # Touch file to change mtime without changing content
        target = sample_repo / "main.py"
        content = target.read_text()
        time.sleep(0.05)
        target.write_text(content)

        stats = manager.index_repo(sample_repo, skip_vectors=True)
        # The touched file should be skipped (hash matches), others skipped by mtime
        assert stats["indexed"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/code_mcp/test_code_manager.py::TestMtimeSkipping -v`
Expected: Tests may pass or fail depending on current behavior. The key test is `test_touched_file_with_same_content_skipped` — currently the code does NOT store mtime, so it will hash every file. The test validates that after this change, the mtime path works correctly.

- [ ] **Step 3: Implement mtime-first skip logic in index_repo**

In `src/code_mcp/code_manager.py`, modify `index_repo()`:

Replace the Phase 1a loop (lines ~87-110) with:

```python
        # Phase 1a: Parse files (mtime-first skip, then hash check)
        parsed_files: list[tuple[str, str, int, str, float | None, list]] = []
        existing_hashes = self.fts_index.get_all_file_hashes(repo_id)
        mtime_updates: list[tuple[str, float]] = []  # files with stale mtime but same hash

        for i, file_path in enumerate(files, 1):
            rel_path = str(file_path.relative_to(repo_path))
            try:
                current_mtime = file_path.stat().st_mtime

                # Fast path: check mtime first
                stored = existing_hashes.get(rel_path)
                if not rebuild and stored is not None:
                    stored_hash, stored_mtime = stored
                    if stored_mtime is not None and stored_mtime == current_mtime:
                        # mtime matches — skip without reading file
                        stats["skipped"] += 1
                        continue

                    # mtime differs — check hash
                    file_hash = CodeFTSIndex.compute_file_hash(file_path)
                    if stored_hash == file_hash:
                        # Content unchanged, just mtime drift — update mtime, skip
                        mtime_updates.append((rel_path, current_mtime))
                        stats["skipped"] += 1
                        continue
                else:
                    file_hash = CodeFTSIndex.compute_file_hash(file_path)

                # File is new or changed — parse it
                result = self._parse_file_for_index(file_path, rel_path, file_hash)
                if result == "failed":
                    stats["failed"] += 1
                else:
                    # Add current_mtime to the tuple for batch write
                    rel_path_r, language, size_bytes, fh, symbols = result
                    parsed_files.append((rel_path_r, language, size_bytes, fh, current_mtime, symbols))
                    stats["indexed"] += 1
                    stats["symbols"] += len(symbols)
            except Exception as e:
                logger.error(f"[{i}/{total}] Failed {rel_path}: {e}")
                stats["failed"] += 1

            if i % 500 == 0 or i == total:
                logger.info(
                    f"  Phase 1a parse: {i}/{total} files "
                    f"({stats['indexed']} parsed, {stats['skipped']} skipped)"
                )

        # Batch-update mtimes for files that had stale mtime but matching hash
        if mtime_updates:
            self.fts_index.update_mtimes_batch(repo_id, mtime_updates)
```

- [ ] **Step 4: Simplify _parse_file_for_index — remove needs_indexing call**

The method no longer needs `repo_id` or `rebuild` params since skip logic is now in `index_repo()`. Simplify its signature:

```python
def _parse_file_for_index(
    self,
    file_path: Path,
    rel_path: str,
    file_hash: str,
) -> tuple[str, str, int, str, list] | str:
    """Parse a file for batch indexing (no DB writes).

    Returns (rel_path, language, size_bytes, file_hash, symbols) or "failed".
    """
    if self.parser.can_parse(file_path) or self.parser.is_doc_file(file_path):
        symbols = self.parser.parse_file(file_path)
    else:
        return "failed"

    if not symbols:
        return "failed"

    language = symbols[0].language
    size_bytes = file_path.stat().st_size
    return (rel_path, language, size_bytes, file_hash, symbols)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/code_mcp/test_code_manager.py -v`
Expected: All tests PASS including new `TestMtimeSkipping` and existing `TestIndexRepo.test_change_detection_skips_unchanged`.

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/code_mcp/ -v`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/code_mcp/code_manager.py tests/code_mcp/test_code_manager.py
git commit -m "perf: mtime-first skip logic in index_repo"
```

---

### Task 4: Add close() Call to CLI and Server

**Files:**
- Modify: `src/code_mcp/cli.py:11-121`
- Modify: `src/code_mcp/server.py:387-423`

- [ ] **Step 1: Add close() to CLI after indexing completes**

In `src/code_mcp/cli.py`, in `index_repos()`, add cleanup before return. After the index stats logging block (line ~119) and before `return`:

```python
    manager.fts_index.close()
    return 0 if total_stats["failed"] == 0 else 1
```

- [ ] **Step 2: Add close() to server shutdown**

In `src/code_mcp/server.py`, in the `run_server()` finally block, add after watcher stop:

```python
    finally:
        if watcher:
            watcher.stop()
            logger.info("Code file watcher stopped")
        manager = _manager
        if manager is not None:
            manager.fts_index.close()
```

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/code_mcp/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/code_mcp/cli.py src/code_mcp/server.py
git commit -m "perf: close persistent SQLite connection on shutdown"
```

---

### Task 5: Incremental Vector Embedding

**Files:**
- Modify: `src/code_mcp/code_embedder.py:174-246` (`add_symbols_batch`)
- Create: `tests/code_mcp/test_code_embedder.py`

- [ ] **Step 1: Write failing tests for incremental embedding**

Create `tests/code_mcp/test_code_embedder.py`:

```python
"""Tests for incremental vector embedding."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_mcp.models import CodeSymbol


def make_symbol(
    name: str = "func",
    text: str = "def func():\n    return 42\n    # padding to exceed MIN_EMBED_CHARS threshold",
    **kwargs,
) -> CodeSymbol:
    return CodeSymbol(
        name=name,
        symbol_type=kwargs.get("symbol_type", "function_definition"),
        text=text,
        start_line=kwargs.get("start_line", 1),
        end_line=kwargs.get("end_line", 3),
        language=kwargs.get("language", "python"),
        file_id=kwargs.get("file_id", 0),
        signature=kwargs.get("signature", f"def {name}():"),
    )


@pytest.fixture()
def embedder():
    """Return a CodeEmbedder with mocked model and DB."""
    from code_mcp.code_embedder import CodeEmbedder

    e = CodeEmbedder.__new__(CodeEmbedder)
    e.model_name = "test-model"
    e._device = "cpu"
    e._backend = "torch"
    e._embedding_dim = 4

    # Mock the model to return fixed-size vectors
    mock_model = MagicMock()
    mock_model.encode.return_value = MagicMock(
        tolist=lambda: [[0.1, 0.2, 0.3, 0.4]] * 10  # up to 10 embeddings
    )
    e._model = mock_model

    # Mock LanceDB
    e._db = MagicMock()

    return e


class TestIncrementalEmbedding:
    def test_creates_table_when_not_exists(self, embedder) -> None:
        embedder._db.table_names.return_value = []

        file_symbols = [(1, [make_symbol(name="func_a")])]
        embedder.add_symbols_batch(file_symbols)

        embedder._db.create_table.assert_called_once()
        args = embedder._db.create_table.call_args
        assert args[0][0] == "code_chunks"

    def test_deletes_then_adds_when_table_exists(self, embedder) -> None:
        mock_table = MagicMock()
        embedder._db.table_names.return_value = ["code_chunks"]
        embedder._db.open_table.return_value = mock_table

        file_symbols = [(1, [make_symbol(name="func_a")]), (2, [make_symbol(name="func_b")])]
        embedder.add_symbols_batch(file_symbols)

        # Should delete old data for file_ids 1 and 2
        mock_table.delete.assert_called_once()
        delete_expr = mock_table.delete.call_args[0][0]
        assert "1" in delete_expr
        assert "2" in delete_expr

        # Should add new data
        mock_table.add.assert_called_once()

    def test_does_not_overwrite_entire_table(self, embedder) -> None:
        """Ensure create_table with mode='overwrite' is NOT called when table exists."""
        embedder._db.table_names.return_value = ["code_chunks"]
        embedder._db.open_table.return_value = MagicMock()

        file_symbols = [(1, [make_symbol()])]
        embedder.add_symbols_batch(file_symbols)

        # create_table should NOT be called (we use open_table + delete + add instead)
        embedder._db.create_table.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/code_mcp/test_code_embedder.py -v`
Expected: FAIL — current implementation always calls `create_table(mode="overwrite")`.

- [ ] **Step 3: Implement selective delete+add in add_symbols_batch**

In `src/code_mcp/code_embedder.py`, replace the table write at the end of `add_symbols_batch` (line ~240-243):

Replace:
```python
        # Write all data as a single new table, replacing any existing one.
        # mode="overwrite" is atomic and avoids the per-file delete loop that
        # created thousands of LanceDB version manifests.
        self.db.create_table(table_name, all_data, mode="overwrite")
```

With:
```python
        # Incremental write: only update changed files, preserve existing embeddings
        if table_name in self.db.table_names():
            table = self.db.open_table(table_name)
            # Delete old embeddings for changed files
            changed_file_ids = list({item[0] for item in all_items})
            id_list = ", ".join(str(fid) for fid in changed_file_ids)
            table.delete(f"file_id IN ({id_list})")
            table.add(all_data)
        else:
            self.db.create_table(table_name, all_data)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/code_mcp/test_code_embedder.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/code_mcp/code_embedder.py tests/code_mcp/test_code_embedder.py
git commit -m "perf: incremental vector embedding with selective delete+add"
```

---

### Task 6: LanceDB Compaction and --compact CLI Flag

**Files:**
- Modify: `src/code_mcp/code_embedder.py`
- Modify: `src/code_mcp/cli.py`
- Test: `tests/code_mcp/test_code_embedder.py`

- [ ] **Step 1: Write failing test for compact method**

Add to `tests/code_mcp/test_code_embedder.py`:

```python
class TestCompaction:
    def test_compact_calls_optimize(self, embedder) -> None:
        mock_table = MagicMock()
        embedder._db.table_names.return_value = ["code_chunks"]
        embedder._db.open_table.return_value = mock_table

        embedder.compact()

        mock_table.compact_files.assert_called_once()

    def test_compact_noop_when_no_table(self, embedder) -> None:
        embedder._db.table_names.return_value = []
        # Should not raise
        embedder.compact()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/code_mcp/test_code_embedder.py::TestCompaction -v`
Expected: FAIL — `compact()` doesn't exist.

- [ ] **Step 3: Implement compact method**

Add to `CodeEmbedder` in `src/code_mcp/code_embedder.py`:

```python
def compact(self) -> None:
    """Compact LanceDB table to defragment after incremental updates."""
    table_name = self.TABLE_NAME
    if table_name not in self.db.table_names():
        logger.info("No vector table to compact")
        return
    table = self.db.open_table(table_name)
    logger.info("Compacting vector index...")
    table.compact_files()
    logger.info("Vector index compaction complete")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/code_mcp/test_code_embedder.py::TestCompaction -v`
Expected: PASS.

- [ ] **Step 5: Add --compact flag to CLI**

In `src/code_mcp/cli.py`, add after the `--background-vectors` argument:

```python
parser.add_argument(
    "--compact",
    action="store_true",
    help="Compact vector index after indexing (defragments LanceDB)",
)
```

Update the `index_repos` function signature to accept `compact: bool = False`:

```python
def index_repos(
    repos_dir: Path | None = None,
    repo_filter: str | None = None,
    rebuild: bool = False,
    verbose: bool = False,
    limit: int | None = None,
    skip_vectors: bool = False,
    background_vectors: bool = False,
    compact: bool = False,
) -> int:
```

At the end of `index_repos`, before the final `return`, add:

```python
    if compact and not skip_vectors:
        try:
            manager.embedder.compact()
        except Exception as e:
            logger.warning(f"Vector compaction failed: {e}")
```

Update the `cli_main` call to pass the new arg:

```python
    sys.exit(
        index_repos(
            repos_dir=args.dir,
            repo_filter=args.repo,
            rebuild=args.rebuild,
            verbose=args.verbose,
            limit=args.limit,
            skip_vectors=args.skip_vectors,
            background_vectors=args.background_vectors,
            compact=args.compact,
        )
    )
```

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/code_mcp/ -v`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/code_mcp/code_embedder.py src/code_mcp/cli.py tests/code_mcp/test_code_embedder.py
git commit -m "feat: add LanceDB compaction with --compact CLI flag"
```

---

### Task 7: Integration Verification

**Files:**
- No new files

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS across all test files.

- [ ] **Step 2: Run linter**

Run: `ruff check src/code_mcp/ tests/code_mcp/`
Expected: No errors.

- [ ] **Step 3: Run formatter**

Run: `ruff format --check src/code_mcp/ tests/code_mcp/`
Expected: No formatting issues.

- [ ] **Step 4: Fix any issues found in steps 1-3, then commit**

```bash
git add -A
git commit -m "chore: fix lint/format issues from incremental indexing changes"
```

(Skip this commit if no issues found.)
