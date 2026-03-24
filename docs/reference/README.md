# Reference Documentation Hub

<!-- SCOPE: Index and navigation for architecture decisions, guides, manuals, and research -->

## Structure

| Directory | Contents |
|-----------|----------|
| [adrs/](adrs/) | Architecture Decision Records |
| [guides/](guides/) | How-to guides for development workflows |
| [manuals/](manuals/) | Detailed technical manuals |
| [research/](research/) | Research notes and technology evaluations |

## Architecture Decision Records

ADRs document significant technical decisions. Use the format:

```
# ADR-NNN: Title

Status: proposed | accepted | deprecated | superseded
Date: YYYY-MM-DD

## Context
[What motivated the decision]

## Decision
[What was decided]

## Consequences
[What follows from the decision]
```

### Key Decisions

| ADR | Decision | Status |
|-----|----------|--------|
| [ADR-001](adrs/adr-001-direct-sqlite-access.md) | Use direct SQLite access instead of Zotero API | Accepted |
| [ADR-002](adrs/adr-002-chromadb-for-vectors.md) | Use ChromaDB for semantic search | Accepted |
| [ADR-003](adrs/adr-003-pymupdf-for-pdf.md) | Use PyMuPDF for PDF processing | Accepted |

---

<!-- Maintenance: Update when new ADRs, guides, or manuals are added -->
