# Task Management

<!-- SCOPE: How work is tracked and prioritized for this project -->

## Workflow

| Status | Meaning |
|--------|---------|
| Todo | Accepted, ready to work on |
| In Progress | Actively being implemented |
| In Review | Implementation done, needs verification |
| Done | Merged and verified |

## Priorities

| Priority | Criteria |
|----------|----------|
| P0 — Critical | Server crashes or data corruption risk |
| P1 — High | Core tool broken or unusable |
| P2 — Medium | Feature improvement or new tool |
| P3 — Low | Documentation, refactoring, nice-to-have |

## Areas

| Area | Scope |
|------|-------|
| `tools/` | MCP tool definitions and behavior |
| `services/zotero` | Zotero SQLite client |
| `services/pdf` | PDF processing and figure detection |
| `services/vector` | ChromaDB and semantic search |
| `infra` | Docker, CI/CD, packaging |
| `docs` | Documentation |

## Contribution Guidelines

1. One feature or fix per branch
2. All tests must pass (`pytest -v`)
3. All lint checks must pass (`ruff check src/ tests/`)
4. Update relevant docs if behavior changes

---

<!-- Maintenance: Update when workflow or priority definitions change -->
