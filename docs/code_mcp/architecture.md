# Architecture

<!-- SCOPE: How the code-mcp server is built — layers, data flow, indexing pipeline -->

## System Context

```
┌──────────────────┐     stdio      ┌─────────────────────────┐
│  Claude Desktop  │◄──────────────►│  code-mcp               │
│  (MCP Client)    │  MCP protocol  │  (MCP Server)           │
└──────────────────┘                └──────────┬──────────────┘
                                               │
                                    ┌──────────┼──────────────┐
                                    │          │              │
                              ┌─────▼─────┐ ┌─▼──────────┐ ┌─▼───────────┐
                              │  SQLite   │ │ LanceDB    │ │ Source Code │
                              │  FTS5     │ │ (vectors)  │ │ Repos       │
                              │  (r/w)    │ │ (r/w)      │ │ (read)      │
                              └───────────┘ └────────────┘ └─────────────┘
```

## Module Architecture

| Module | File | Responsibility |
|--------|------|----------------|
| **Config** | `config.py` | Pydantic settings from `CODE_MCP_*` env vars |
| **Models** | `models.py` | Data classes: `CodeRepo`, `CodeSymbol`, `ExtractedCodeFile` |
| **Parser** | `parser.py` | Tree-sitter symbol extraction + doc file chunking |
| **FTS Index** | `code_fts.py` | SQLite FTS5 keyword index with triggers |
| **Embedder** | `code_embedder.py` | sentence-transformers + LanceDB vector storage |
| **Manager** | `code_manager.py` | Facade coordinating parser, FTS, and embedder |
| **Watcher** | `code_watcher.py` | Watchdog-based incremental reindexing |
| **Server** | `server.py` | MCP server with tool handlers |
| **CLI** | `cli.py` | `code-mcp-index` batch indexing command |

## Indexing Pipeline

```
code-mcp-index (CLI)
  │
  ▼
CodeIndexManager.index_repo(repo_path)
  │
  ├── Phase 1a: Parse ──────────────────────────────────────────┐
  │   for each file:                                            │
  │     compute SHA256 hash                                     │
  │     skip if hash unchanged (change detection)               │
  │     TreeSitterParser.parse_file() → list[CodeSymbol]        │
  │                                                             │
  ├── Phase 1b: Batch FTS write ────────────────────────────────┤
  │   CodeFTSIndex.add_files_batch()                            │
  │     suspend FTS triggers                                    │
  │     bulk INSERT symbols                                     │
  │     selective FTS populate (new rows only)                   │
  │     restore triggers                                        │
  │                                                             │
  └── Phase 2: Vector embed (optional) ─────────────────────────┘
      CodeEmbedder.add_symbols_batch()
        encode (name + signature + text[:1500])
        store in LanceDB
        can run in background thread
```

## Search Modes

### Keyword Search (FTS5)
- Porter stemming + unicode61 tokenizer
- Searches symbol names and source text
- Fast, deterministic, good for exact matches

### Semantic Search (Vector)
- sentence-transformers encoding (`all-MiniLM-L6-v2` default)
- LanceDB approximate nearest neighbor lookup
- Supports E5 asymmetric query/passage prefixes
- Best for conceptual queries

### Hybrid Search (RRF)
- Runs keyword + semantic in parallel (`asyncio.gather`)
- Combines via Reciprocal Rank Fusion (k=60)
- Deduplicates and ranks by fused score

## FTS5 Schema

```sql
repositories (repo_id, name, path, indexed_at)
    │
    ▼
code_files (file_id, repo_id, rel_path, language, file_hash, size_bytes)
    │
    ▼
code_symbols (symbol_id, file_id, symbol_name, symbol_type,
              signature, text, start_line, end_line, language, parent_name)
    │
    ▼
code_symbols_fts (FTS5 virtual table, synced via triggers)
```

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| FTS engine | SQLite FTS5 | Zero-dependency, WAL mode for concurrent access |
| Vector DB | LanceDB | Embedded, no server process, fast ANN search |
| Parser | tree-sitter | Language-agnostic AST, supports 13+ languages |
| Batch indexing | Suspend triggers + rebuild | 10x faster than per-row trigger updates |
| Change detection | SHA256 file hash | Skip unchanged files without re-parsing |
| Embedding cap | 1500 chars | Fits within model token limits (512 tokens) |
| Fallback | Graceful degradation | Works without tree-sitter or embeddings |
| File watching | Watchdog + debounce | Incremental reindex, 2s cooldown prevents thrashing |

---

<!-- Maintenance: Update when indexing pipeline or search logic changes -->
