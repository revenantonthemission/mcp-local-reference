# Architecture

<!-- SCOPE: How the server is built — layers, data flow, service responsibilities -->

## System Context

```
┌──────────────────┐     stdio      ┌─────────────────────────┐
│  Claude Desktop  │◄──────────────►│  mcp-local-reference    │
│  (MCP Client)    │  MCP protocol  │  (MCP Server)           │
└──────────────────┘                └──────────┬──────────────┘
                                               │
                                    ┌──────────┼──────────────┐
                                    │          │              │
                              ┌─────▼─────┐ ┌─▼──────┐ ┌─────▼─────┐
                              │  Zotero   │ │ Zotero │ │ ChromaDB  │
                              │  SQLite   │ │ PDFs   │ │ (vector)  │
                              │  (read)   │ │ (read) │ │ (r/w)     │
                              └───────────┘ └────────┘ └───────────┘
```

## Layer Architecture

| Layer | Directory | Responsibility |
|-------|-----------|----------------|
| **Entry point** | `__main__.py` | Creates server, runs stdio transport |
| **Server** | `server.py` | FastMCP instance, registers all tools |
| **Tools** | `tools/` | MCP tool definitions — input validation, response formatting |
| **Services** | `services/` | Business logic — data access, processing, embedding |
| **Config** | `config.py` | Environment-based configuration with defaults |

## Service Responsibilities

| Service | File | What it does |
|---------|------|-------------|
| `ZoteroClient` | `services/zotero_client.py` | Read-only SQLite queries against Zotero's database |
| `PdfProcessor` | `services/pdf_processor.py` | Text extraction, figure detection, page rendering via PyMuPDF |
| `VectorStore` | `services/vector_store.py` | ChromaDB index management and semantic search |

## Tool-to-Service Mapping

| Tool Module | Services Used |
|-------------|---------------|
| `tools/references.py` | ZoteroClient, VectorStore |
| `tools/pdf_reader.py` | ZoteroClient, PdfProcessor |
| `tools/figures.py` | ZoteroClient, PdfProcessor |

## Data Flow: Search References

```
Claude calls search_references("machine learning", semantic=True)
  │
  ▼
tools/references.py
  │
  ├─ vector_store.is_indexed()? ──Yes──► vector_store.search(query)
  │                                          │
  │                                          ▼
  │                                     ChromaDB cosine similarity
  │                                          │
  │                                          ▼
  │                                     For each result:
  │                                       zotero.get_reference(key)
  │
  └─ No index ──► zotero.search(query)
                      │
                      ▼
                  SQLite LIKE query on itemDataValues
```

## Data Flow: Crop Figure

```
Claude calls list_figures(item_key)
  │
  ▼
zotero.get_pdf_path(item_key)
  │
  ▼
pdf_processor.detect_figures(pdf_path)
  │
  ▼
Returns [{page, bbox, size}, ...]

Claude calls crop_figure(item_key, page=2, x0=50, y0=100, x1=400, y1=350)
  │
  ▼
pdf_processor.render_page_region(path, page, bbox, dpi=300)
  │
  ▼
Returns PNG bytes → MCP Image(data=bytes, format="png")
```

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Transport | stdio | Standard for Claude Desktop local servers |
| Zotero access | Direct SQLite | No dependency on running Zotero app; fastest read path |
| SQLite mode | Read-only (`?mode=ro`) | Safe concurrent access while Zotero is running |
| Connection pattern | Per-call | Avoids stale handles; Zotero may modify DB between calls |
| Vector DB | ChromaDB (embedded) | No separate process; ONNX embeddings included |
| Image return | MCP Image (base64 PNG) | Claude can display inline; no file system side effects |
| Tool registration | `register_tools(mcp, config)` per module | Avoids circular imports; clean dependency injection |

---

<!-- Maintenance: Update when new services, tools, or data flows are added -->
