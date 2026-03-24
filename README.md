# mcp-local-reference

An MCP server that gives Claude (or any MCP client) read access to your **Zotero** library. Search references, extract PDF text, crop figures, and format Harvard (Cite Them Right) citations — all from within your writing workflow.

## Features

| Tool | Description |
|------|-------------|
| `search_references` | Keyword or semantic search across your Zotero library |
| `get_reference` | Full metadata for a single reference |
| `list_collections` | Browse Zotero collections as a tree |
| `get_collection_items` | List references in a collection |
| `get_pdf_text` | Extract text from a reference's PDF |
| `list_figures` | Detect figures/images in a PDF |
| `crop_figure` | Crop a region from a PDF page and return as PNG |
| `format_citation` | Harvard Cite Them Right formatted citation |
| `index_library` | Build a vector index for semantic search |

## Prerequisites

- **Zotero** installed with a local library (the server reads `zotero.sqlite` directly)
- **Python 3.11+**
- **uv** (recommended) or pip

## Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/mcp-local-reference.git
cd mcp-local-reference

# Install with uv
uv pip install -e .

# Or with pip
pip install -e .
```

## Claude Desktop Configuration

Add the server to your Claude Desktop config file:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

### Using uv (recommended)

```json
{
  "mcpServers": {
    "local-reference": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-local-reference", "mcp-local-reference"]
    }
  }
}
```

### Using pip

```json
{
  "mcpServers": {
    "local-reference": {
      "command": "mcp-local-reference"
    }
  }
}
```

### Using Docker

```json
{
  "mcpServers": {
    "local-reference": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "~/Zotero:/zotero",
        "-v", "mcp-local-ref-data:/data",
        "mcp-local-reference"
      ]
    }
  }
}
```

## Configuration

All settings are via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ZOTERO_DATA_DIR` | `~/Zotero` | Path to your Zotero data directory |
| `MCP_DATA_DIR` | `~/.mcp-local-reference` | Path for the vector index and other data |
| `FIGURE_DPI` | `300` | Resolution for cropped figures |
| `MIN_FIGURE_PIXELS` | `10000` | Minimum pixel area to count as a figure |

## Usage

Once connected to Claude Desktop, you can ask Claude things like:

- *"Search my Zotero library for papers about transformer architectures"*
- *"Get the abstract of reference TESTKEY1"*
- *"Extract the text from pages 5-10 of this paper"*
- *"List the figures in this PDF and crop figure 3"*
- *"Format a Harvard citation for this reference"*
- *"Index my library for semantic search"*

### Semantic search

Run `index_library` once to build a vector index. After that, `search_references` uses semantic similarity instead of keyword matching — so queries like *"papers about attention mechanisms in neural networks"* find relevant results even if those exact words aren't in the title.

## Development

```bash
# Install with dev dependencies
uv pip install -e ".[dev]"

# Run tests
pytest -v

# Lint
ruff check src/ tests/

# Format
ruff format src/ tests/
```

## License

MIT
