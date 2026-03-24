# Tech Stack

<!-- SCOPE: Technology choices, versions, and justifications -->

## Runtime

| Component | Version | Purpose |
|-----------|---------|---------|
| Python | >= 3.11 | Runtime (type hint features: `X \| None`, `list[T]`) |
| Hatchling | latest | Build backend (PEP 517 compliant) |

## Core Dependencies

| Package | Version | Purpose | Why this one |
|---------|---------|---------|-------------|
| `mcp[cli]` | >= 1.0.0 | MCP server framework | Official Python SDK from Anthropic |
| `pymupdf` | >= 1.25.0 | PDF text extraction + figure detection + page rendering | Best Python PDF library for image extraction with bounding boxes |
| `chromadb` | >= 0.6.0 | Vector store for semantic search | Embedded mode, ships with ONNX embeddings (no PyTorch needed) |

## Dev Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pytest` | >= 8.0.0 | Test framework |
| `ruff` | >= 0.8.0 | Linting + formatting (replaces flake8 + black + isort) |
| `mypy` | >= 1.13.0 | Static type checking (strict mode) |

## External Data Sources

| Source | Type | Access |
|--------|------|--------|
| Zotero SQLite (`zotero.sqlite`) | SQLite 3 database | Read-only, file URI with `?mode=ro` |
| Zotero storage (`storage/`) | File system | Read-only PDF files |
| ChromaDB (`chroma/`) | Embedded vector DB | Read-write, persistent to disk |

## Infrastructure

| Component | Technology |
|-----------|-----------|
| Container | Docker (python:3.11-slim) |
| CI/CD | GitHub Actions (lint + test) |
| Package format | Python wheel (src layout) |
| MCP transport | stdio |

## Embedding Model

ChromaDB's default embedding function: `all-MiniLM-L6-v2` via ONNX runtime

| Property | Value |
|----------|-------|
| Model size | ~80 MB |
| Dimensions | 384 |
| Max tokens | 256 |
| Runtime | ONNX (no GPU required) |

---

<!-- Maintenance: Update when dependency versions change or new dependencies are added -->
