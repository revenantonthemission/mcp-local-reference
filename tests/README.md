# Test Documentation

<!-- SCOPE: Test strategy, structure, and how to run tests -->

## Running Tests

| Command | Purpose |
|---------|---------|
| `pytest -v` | Run all tests with verbose output |
| `pytest tests/test_zotero_client.py` | Run only Zotero client tests |
| `pytest tests/code_mcp/` | Run only code-mcp tests |
| `pytest -k "test_search"` | Run tests matching a keyword |

## Test Structure

### mcp-local-reference

| File | Tests | Fixture |
|------|-------|---------|
| `test_zotero_client.py` | Search, get reference, collections, get all | Mock Zotero SQLite DB |
| `test_pdf_processor.py` | Text extraction, page count, region rendering | Generated single-page PDF |

### code-mcp (`tests/code_mcp/`)

| File | Tests | Fixture |
|------|-------|---------|
| `test_parser.py` | Symbol extraction, doc chunking, fallback, edge cases | Temp source files |
| `test_code_fts.py` | Repo/file CRUD, FTS5 search, batch insert, stats | Temp SQLite DB |
| `test_code_manager.py` | Indexing, keyword search, exclusions, reindex, remove | Mock repo + mock embedder |

## Test Strategy

### What We Test

| Layer | Approach |
|-------|----------|
| Services (`services/`) | Unit tests with mock data (SQLite fixture, generated PDFs/images) |
| Tools (`tools/`) | Not directly tested — thin wrappers over services |
| Config (`config.py`) | Tested implicitly via service tests |
| Server (`server.py`) | Smoke-tested by running `python -m mcp_local_reference` |
| code-mcp Parser | Unit tests with temp files; tree-sitter tests skipped if not installed |
| code-mcp FTS Index | Unit tests with temp SQLite database |
| code-mcp Manager | Integration tests with real parser + FTS, mocked embedder |

### Mock Zotero Database

The `conftest.py` fixture creates an in-memory SQLite database with:

| Test Data | Records |
|-----------|---------|
| Journal article | "Deep Learning for NLP" — Smith & Jones, 2024, with DOI and tags |
| Book | "AI: A Modern Approach" — Russell & Norvig, 2021, Pearson |
| Collection | "Machine Learning" containing both items |
| Item types | journalArticle, book, attachment, note, conferencePaper |
| Fields | title, abstract, DOI, date, publication, volume, issue, pages, etc. |

### What We Don't Test

| Area | Reason |
|------|--------|
| ChromaDB integration | Requires embedding model download; tested manually |
| Real Zotero database | User-specific; no mock can cover all schema variations |
| MCP protocol transport | Covered by the `mcp` SDK's own tests |
| Docker builds | Tested in CI pipeline |
| code-mcp vector embeddings | Requires `sentence-transformers` + `lancedb`; mocked in tests |
| code-mcp file watcher | Requires filesystem events with timing; tested manually |

## Adding Tests

1. Add test data to `conftest.py` (new items, collections, or attachments)
2. Write tests in the appropriate `test_*.py` file
3. Use the `config` fixture to get a `Config` pointing at the mock DB
4. Run `pytest -v` to verify

---

<!-- Maintenance: Update when new test files or fixtures are added -->
