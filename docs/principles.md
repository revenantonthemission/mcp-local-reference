# Development Principles

<!-- SCOPE: Coding standards and development principles for this project -->

## Core Principles

| # | Principle | Application |
|---|-----------|-------------|
| 1 | **Read-only access** | Never write to Zotero's database or storage; always open SQLite in `?mode=ro` |
| 2 | **Fail gracefully** | Missing PDFs, empty results, and absent indexes should return informative messages, not crash the server |
| 3 | **Connection-per-call** | Create fresh SQLite connections for each public method; do not hold long-lived handles |
| 4 | **Stateless tools** | Each MCP tool call is independent; no session state between calls |
| 5 | **Base64 for images** | Return cropped figures as base64 PNG via MCP Image type; no file-system side effects |
| 6 | **YAGNI** | Only build what the MCP tools need; do not add web endpoints, admin UIs, or unused abstractions |
| 7 | **Type safety** | `from __future__ import annotations` in every module; strict mypy |
| 8 | **Testable services** | Services accept a `Config` object; tests use a mock Zotero SQLite fixture |

## Code Conventions

| Convention | Rule |
|------------|------|
| Python version | 3.11+ required |
| Type hints | All function signatures, strict mypy |
| Imports | `from __future__ import annotations` at top of every module |
| Formatting | ruff format (line length 100) |
| Linting | ruff check with E, F, I, N, W, UP rules |
| Tool registration | `register_tools(mcp, config)` function per tool module |
| Service pattern | Stateless classes with `Config` injected via constructor |
| Tests | pytest with mock SQLite DB in `conftest.py` |

## Anti-Patterns to Avoid

| Anti-Pattern | Why |
|--------------|-----|
| Writing to Zotero's SQLite | Corrupts user's library; violates read-only contract |
| Singleton SQLite connections | Stale handles when Zotero modifies its DB |
| Global mutable state | Breaks testability and concurrent tool calls |
| Embedding API keys in code | ChromaDB uses local ONNX embeddings; no external APIs needed |
| Saving cropped images to disk | MCP Image type returns base64 directly to Claude |

---

<!-- Maintenance: Update when new conventions are established or principles change -->
