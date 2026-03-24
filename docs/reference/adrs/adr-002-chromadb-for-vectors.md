# ADR-002: ChromaDB for Semantic Search

**Status:** Accepted
**Date:** 2025-03-24

## Context

Semantic search requires a vector database to store and query embeddings. Options considered: ChromaDB (embedded), FAISS (library), Qdrant (server), SQLite-VSS (extension).

## Decision

Use ChromaDB in embedded (PersistentClient) mode with its default ONNX-based embedding function (`all-MiniLM-L6-v2`).

## Consequences

| Aspect | Impact |
|--------|--------|
| Simplicity | No separate server process; single `pip install` |
| Embeddings | Built-in ONNX model (~80MB) — no API keys or GPU needed |
| Persistence | Writes to disk at `MCP_DATA_DIR/chroma/` |
| Performance | Sufficient for typical Zotero libraries (1K–50K references) |
| Trade-off | Heavier dependency than FAISS; lighter than Qdrant |
