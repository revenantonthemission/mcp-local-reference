# Functional Requirements

<!-- SCOPE: What the MCP server does — tools, inputs, outputs, user workflows -->

## MCP Tools

| Tool | Input | Output | Purpose |
|------|-------|--------|---------|
| `search_references` | query, limit, semantic | JSON array of {key, title, authors, year} | Find references by keyword or meaning |
| `get_reference` | item_key | JSON object with full metadata | Retrieve complete reference details |
| `list_collections` | — | JSON tree of collections | Browse Zotero folder structure |
| `get_collection_items` | collection_key, limit | JSON array of references | List items in a collection |
| `get_pdf_text` | item_key, start_page, end_page | Extracted text by page | Read PDF content |
| `list_figures` | item_key | JSON array of {page, bbox, size} | Detect figures in a PDF |
| `crop_figure` | item_key, page, x0, y0, x1, y1, dpi | PNG image (base64 via MCP Image) | Extract a figure region |
| `format_citation` | item_key | Formatted citation string | Harvard Cite Them Right citation |
| `index_library` | — | JSON {status, indexed count} | Build vector index for semantic search |

## User Workflows

### Blog Writing with References

1. User asks Claude to write about a topic
2. Claude calls `search_references` to find relevant papers
3. Claude calls `get_reference` and `get_pdf_text` to read content
4. Claude calls `crop_figure` to extract a relevant figure
5. Claude calls `format_citation` to generate proper citations
6. Claude writes the blog post with citations and figures

### First-Time Setup

1. User installs the server and configures Claude Desktop
2. User asks Claude to call `index_library`
3. Server reads all Zotero references, embeds title+abstract+tags
4. Semantic search is now available for all future queries

## Data Access Contracts

| Data Source | Access Mode | Constraint |
|-------------|-------------|------------|
| Zotero SQLite | Read-only (`?mode=ro`) | Database must exist at configured path |
| Zotero storage/ | Read-only (file system) | PDFs resolved via attachment key |
| ChromaDB | Read-write (embedded) | Stored at `MCP_DATA_DIR/chroma/` |

## Configuration

See [Infrastructure — Environment Variables](infrastructure.md#environment-variables) for all configuration options.

---

<!-- Maintenance: Update when MCP tools are added/modified or configuration changes -->
