# Documentation Hub

<!-- SCOPE: Central navigation for all project documentation -->

> **mcp-local-reference** — MCP server for accessing Zotero references, extracting PDF text, and cropping figures.

## Quick Navigation

| Document | Purpose |
|----------|---------|
| [Requirements](project/requirements.md) | What the server does — tools, inputs, outputs |
| [Architecture](project/architecture.md) | How it's built — layers, data flow, service design |
| [Tech Stack](project/tech_stack.md) | Technology choices and versions |
| [Infrastructure](project/infrastructure.md) | Docker, CI/CD, environment configuration |

## Reference

| Resource | Purpose |
|----------|---------|
| [Reference Hub](reference/README.md) | ADRs, guides, manuals, research |
| [Tasks](tasks/README.md) | Task management workflow |
| [Principles](principles.md) | Development principles and coding standards |
| [Documentation Standards](documentation_standards.md) | How to maintain these docs |
| [Presentation](presentation/README.md) | Project overview slide deck |
| [Test Strategy](../tests/README.md) | Test structure, fixtures, how to run tests |

## Project Overview

mcp-local-reference is an MCP (Model Context Protocol) server designed for research-informed blog writing. It connects Claude Desktop to your local Zotero library, providing:

- **Reference search** — keyword and semantic (vector) search across your Zotero library
- **PDF text extraction** — read content from referenced papers and books
- **Figure cropping** — detect and extract figures from academic PDFs as images
- **Citation formatting** — Harvard Cite Them Right style citations

## Getting Started

1. Install: `pip install -e .` (or `uv pip install -e .`)
2. Configure Claude Desktop (see [Infrastructure](project/infrastructure.md))
3. Run `index_library` tool to enable semantic search
4. Ask Claude to search, cite, and crop from your Zotero library

---

<!-- Maintenance: Update when new documentation sections are added -->
