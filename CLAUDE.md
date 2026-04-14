<!-- SCOPE: Project summary, structure, key commands, and conventions for AI coding assistants -->

# mcp-local-reference

MCP server for accessing Zotero references, PDFs, and figures, plus a source code indexing & search server. Built with Python + FastMCP / MCP SDK.

> Full documentation: [docs/README.md](docs/README.md)

## Project Structure

- `src/mcp_local_reference/` — package root
  - `server.py` — FastMCP server creation
  - `config.py` — env-var based configuration
  - `tools/` — MCP tool definitions (references, pdf_reader, figures, local_pdf)
  - `services/` — business logic (zotero_client, pdf_processor, vector_store)
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
- **Zotero access:** Read-only SQLite with `?mode=ro`
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
- Tools are registered via `register_tools(mcp, config)` functions
- Services are stateless (connections created per-call for Zotero)
- Tests use a mock SQLite DB in `conftest.py` — no real Zotero needed
- code_mcp tests use `tests/code_mcp/conftest.py` with sample repo fixture

<!-- Maintenance: Update when project structure, commands, or conventions change -->
