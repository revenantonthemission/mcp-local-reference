# ADR-001: Direct SQLite Access for Zotero

**Status:** Accepted
**Date:** 2025-03-24

## Context

Zotero provides three access paths: local REST API (requires Zotero running on port 23119), Web API (requires API key, rate-limited), and direct SQLite file access.

## Decision

Use direct read-only SQLite access (`?mode=ro`) to Zotero's `zotero.sqlite` database and file system access to `storage/` for PDFs.

## Consequences

| Aspect | Impact |
|--------|--------|
| Speed | Fastest possible reads — no network overhead |
| Availability | Works offline; Zotero does not need to be running |
| Safety | Read-only mode prevents accidental writes |
| Coupling | Tied to Zotero's internal schema (stable across versions) |
| Concurrency | SQLite WAL mode allows reads while Zotero writes |
