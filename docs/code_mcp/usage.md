# Usage

<!-- SCOPE: Configuration, CLI commands, and search modes for code-mcp -->

## Configuration

All settings use environment variables with the `CODE_MCP_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `CODE_MCP_REPOS_DIR` | `~/source-codes` | Directory containing git repositories |
| `CODE_MCP_DATA_DIR` | `~/.local/share/code-mcp` | Index storage location |
| `CODE_MCP_MAX_FILE_SIZE_KB` | `100` | Skip files larger than this (KB) |
| `CODE_MCP_MAX_DOC_CHUNK_LINES` | `200` | Split large docs by headings above this threshold |
| `CODE_MCP_WATCH_ENABLED` | `true` | Enable file system monitoring |
| `CODE_MCP_WATCH_DEBOUNCE_SECONDS` | `2.0` | Cooldown between reindex operations |
| `CODE_MCP_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model name |
| `CODE_MCP_EMBEDDING_DEVICE` | `auto` | Device: `auto`, `cpu`, `mps`, `cuda` |
| `CODE_MCP_EMBEDDING_BACKEND` | `torch` | Backend: `torch`, `onnx`, `openvino` |
| `CODE_MCP_EMBEDDING_BATCH_SIZE` | `256` | Symbols per embedding batch |

Settings can also be placed in a `.env` file in the project root.

## CLI: `code-mcp-index`

Batch index source code repositories before using the MCP server.

```bash
# Index all repos in ~/source-codes
code-mcp-index

# Index a specific repo
code-mcp-index --repo linux

# Custom directory with full rebuild
code-mcp-index --dir ~/projects --rebuild

# Fast indexing (keyword search only, no embeddings)
code-mcp-index --skip-vectors

# Non-blocking: FTS available immediately, vectors build in background
code-mcp-index --background-vectors

# Test with limited files
code-mcp-index --repo cpython --limit 100 --verbose
```

| Flag | Description |
|------|-------------|
| `--dir PATH` | Custom repos directory |
| `--repo NAME` | Only index this specific repository |
| `--rebuild` | Force full rebuild (ignore change detection) |
| `--skip-vectors` | Skip vector embeddings (FTS keyword search only) |
| `--background-vectors` | Run vector embedding in background thread |
| `--limit N` | Max files per repo (for testing) |
| `-v, --verbose` | Enable debug logging |

## MCP Tools

### `search_code`

Search indexed source code repositories.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | (required) | Function names, keywords, or concepts |
| `search_type` | enum | `"hybrid"` | `"hybrid"`, `"keyword"`, or `"semantic"` |
| `repos` | string[] | all | Filter by repository names |
| `languages` | string[] | all | Filter by programming languages |
| `symbol_types` | string[] | all | Filter by symbol type (`"function"`, `"class"`, etc.) |
| `limit` | int | 20 | Max results (capped at 100) |

### `list_repos`

List all indexed repositories with file and symbol counts. No parameters.

### `get_index_status`

Get index statistics (repo/file/symbol counts, index sizes). No parameters.

### `get_symbol`

Browse symbols in an indexed file.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `repo` | string | (required) | Repository name |
| `path` | string | (required) | Relative file path within the repo |
| `name` | string | (omit for overview) | Symbol name to retrieve full source |

**Without `name`**: Lists all symbols in the file (overview mode).
**With `name`**: Returns the full source code of the matching symbol(s).

## File Watching

When `CODE_MCP_WATCH_ENABLED=true` (default), the server monitors all indexed repositories for file changes using watchdog. Changes are debounced (2s default) and trigger incremental reindexing.

Watched events:
- **File created/modified**: Reindex the file
- **File deleted**: Remove from index

Excluded paths: `.git`, `node_modules`, `__pycache__`, `vendor`, `target`, `build`, `dist`, and hidden files.

## Graceful Degradation

The server works with reduced functionality when optional dependencies are missing:

| Missing Dependency | Effect |
|-------------------|--------|
| `tree-sitter-language-pack` | Falls back to file-level chunking (no symbol extraction) |
| `sentence-transformers` | Semantic and hybrid search unavailable; keyword search still works |
| `lancedb` | No vector storage; keyword-only search |
| `watchdog` | No file watching; manual re-index required |

---

<!-- Maintenance: Update when new tools or configuration options are added -->
