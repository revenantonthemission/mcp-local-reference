# Code MCP Server

<!-- SCOPE: Documentation hub for the code-mcp standalone MCP server -->

> **code-mcp** — MCP server for symbol-level search over local source code repositories.

## Quick Navigation

| Document | Purpose |
|----------|---------|
| [Architecture](architecture.md) | How it's built — layers, data flow, indexing pipeline |
| [Usage](usage.md) | Configuration, CLI commands, search modes |

## Overview

Code MCP is a standalone MCP server that provides **symbol-level search** over local source code repositories. It combines three indexing strategies:

1. **TreeSitter parsing** — Extract functions, classes, methods from source code ASTs
2. **FTS5 full-text search** — Fast keyword-based discovery in SQLite
3. **Vector embeddings** — Semantic/concept-based search using sentence-transformers + LanceDB

## MCP Tools

| Tool | Purpose |
|------|---------|
| `search_code` | Search indexed repos by keyword, semantic similarity, or hybrid (RRF) |
| `list_repos` | List all indexed repositories with file/symbol counts |
| `get_index_status` | Get index statistics (sizes, counts) |
| `get_symbol` | Browse symbols in a file (overview) or retrieve full source by name |

## Supported Languages

Python, JavaScript, TypeScript, TSX, Go, Rust, C, C++, Java, C#, Ruby, Kotlin, PHP, Swift — plus Markdown, RST, and TXT documentation files.

## Getting Started

1. Install dependencies: `uv pip install -e ".[dev]"`
2. Place source repos in `~/source-codes/` (or set `CODE_MCP_REPOS_DIR`)
3. Index: `code-mcp-index`
4. Configure Claude Desktop to use the `code-mcp` server

---

<!-- Maintenance: Update when new tools or features are added -->
