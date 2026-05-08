<!-- SCOPE: Project summary, structure, key commands, and conventions for AI coding assistants -->

# mcp-local-reference

MCP server for accessing Zotero references, PDFs, and figures, plus a source code indexing & search server. Built with Python + FastMCP / MCP SDK.

> Full documentation: [docs/README.md](docs/README.md)

## Project Structure

- `src/mcp_local_reference/` — package root
  - `server.py` — FastMCP server creation
  - `config.py` — `pydantic_settings.BaseSettings` (auto-loads project-root `.env`)
  - `tools/` — MCP tool definitions (references, pdf_reader, figures, local_pdf, auto_tag)
  - `services/` — business logic (zotero_client, zotero_api_client, pdf_processor, vector_store)
- `src/code_mcp/` — source code indexing & search MCP server
  - `server.py` — MCP server (search_code, list_repos, get_symbol tools)
  - `cli.py` — `code-mcp-index` CLI for indexing repos
  - `code_manager.py` — indexing facade (2-phase: FTS then vector embedding)
  - `code_fts.py` — SQLite FTS5 index with persistent connection
  - `code_embedder.py` — LanceDB vector embeddings via sentence-transformers
  - `parser.py` — tree-sitter symbol extraction
- `tests/` — pytest suite with mock Zotero DB fixture

## Key Commands

```bash
# Install
uv pip install -e ".[dev]"
# Install with code search deps (tree-sitter, sentence-transformers, lancedb)
uv pip install -e ".[dev,full]"

# Test
uv run pytest -v

# Lint
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Run server directly
python -m mcp_local_reference

# Index source code repos (requires [full] extras)
uv run code-mcp-index
uv run code-mcp-index --skip-vectors  # FTS only, ~10x faster
```

## Architecture

### mcp_local_reference
- **Transport:** stdio (for Claude Desktop)
- **Zotero read access:** Read-only SQLite with `?mode=ro` (queries always go through `services/zotero_client.py`)
- **Zotero write access:** Web API via httpx (`services/zotero_api_client.py`) — writes go to `api.zotero.org` and propagate back via Zotero sync, so the local SQLite stays read-only end-to-end
- **Auto-tagging (`tools/auto_tag.py`):** three MCP tools designed for human-in-the-loop tagging — `suggest_tags_context` (local read; returns title + abstract + current tags + top-30 vocabulary), `apply_tags` (Web API write; append-only set union, `dry_run=True` by default with cap of 25 tags, optimistic concurrency via `If-Unmodified-Since-Version`), and `remove_tags` (Web API write; mirror of `apply_tags` for removal — same dry-run + cap + concurrency semantics, idempotent on tags not present). Dry-run for both write tools reads from local SQLite (no creds needed); real writes require `ZOTERO_USER_ID` + `ZOTERO_API_KEY`
- **Collection editing (`tools/collections.py`):** seven MCP tools mirroring the auto-tagging discipline — six write tools (`create_collection`, `rename_collection`, `reparent_collection`, `delete_collection`, `add_items_to_collection`, `remove_items_from_collection`) and one read tool (`suggest_collection_placement`). All write tools default to `dry_run=True` with optimistic concurrency via `If-Unmodified-Since-Version`; item-membership tools cap input at 25 items per call (`MAX_ITEMS_PER_CALL`) and have **partial-failure semantics** (each item gets its own PATCH, so item 7 of 25 failing leaves 1–6 applied and continues 8–25). `delete_collection` previews items and child collections that would be orphaned (Zotero's API doesn't cascade-delete). `reparent_collection` detects cycles locally before any API call. `suggest_collection_placement` is read-only and returns the item's title + abstract (PDF snippet fallback), the full collection tree, and per-collection item counts so Claude can advise placement without writing.
- **Adding references (`tools/add_reference.py`):** three MCP tools — `add_reference_by_doi`, `add_reference_by_arxiv`, `add_reference_by_isbn` — that resolve a single canonical identifier through an external API (Crossref / arXiv / Open Library), dedupe against the local SQLite (skip-and-warn: returns the existing `item_key` with `status: "exists"` rather than POSTing a duplicate), and create a new item via the Web API. The arXiv tool additionally fetches and attaches the open-access PDF as a child item (non-fatal — if the PDF download or upload fails, the metadata item is still created with `pdf_status: "failed"`). All three tools default to `dry_run=True` and call the resolver even in dry-run so the LLM-facing preview shows real metadata. Optional `collection_key` is filed atomically as part of the create POST. Long-tail items without a DOI/arXiv/ISBN remain manually curated through Zotero's desktop UI.
- **Configuration:** `pydantic_settings.BaseSettings` with `env_file` resolved from `__file__` so `.env` loads regardless of cwd. `extra="ignore"` lets it coexist with `code_mcp.Settings` on a shared `.env`
- **Vector search:** ChromaDB with default ONNX embeddings
- **PDF processing:** PyMuPDF (fitz)
- **Image processing:** Pillow
- **Citation style:** Harvard Cite Them Right

### code_mcp
- **Transport:** stdio (raw MCP SDK `Server`, not FastMCP)
- **Indexing:** 2-phase — Phase 1: tree-sitter parsing + SQLite FTS5, Phase 2: vector embedding
- **FTS:** SQLite FTS5 with persistent connection, mtime-based incremental skip
- **Vector search:** LanceDB with sentence-transformers (default: all-MiniLM-L12-v2)
- **Config:** `CODE_MCP_` env prefix via pydantic-settings

## Conventions

- Python 3.11+, type hints everywhere
- Line length: 100 (enforced by ruff)
- `from __future__ import annotations` in every module
- Tools are registered via `register_tools(mcp, config)` functions; tool implementations live in module-level `*_impl()` helpers outside the closure so they're directly unit-testable without an MCP harness
- Services are stateless (connections created per-call for Zotero)
- Credentials (`ZOTERO_API_KEY`, etc.) live in a project-root `.env` (gitignored) — never default a credential field to a non-empty literal in `config.py`
- Tests use a mock SQLite DB in `conftest.py` — no real Zotero needed
- code_mcp tests use `tests/code_mcp/conftest.py` with sample repo fixture
- SQLite FK columns must have explicit indexes (SQLite doesn't auto-create them unlike PostgreSQL)
- Non-trivial feature work flows through `superpowers:brainstorming` → `superpowers:writing-plans` → `superpowers:subagent-driven-development` on a `feat/*` branch, fast-forward merged back to `main`. Specs live in `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`, plans in `docs/superpowers/plans/YYYY-MM-DD-<topic>.md`.

<!-- Maintenance: Update when project structure, commands, or conventions change -->
