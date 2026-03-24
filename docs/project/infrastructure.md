# Infrastructure

<!-- SCOPE: Docker setup, CI/CD pipeline, environment configuration, deployment -->

## Claude Desktop Configuration

| Transport | Config file (macOS) |
|-----------|-------------------|
| stdio | `~/Library/Application Support/Claude/claude_desktop_config.json` |

### Direct Python (development)

| Key | Value |
|-----|-------|
| command | `/path/to/.venv/bin/python` |
| args | `["-m", "mcp_local_reference"]` |

### Docker (distribution)

| Key | Value |
|-----|-------|
| command | `docker` |
| args | `["run", "-i", "--rm", "-v", "~/Zotero:/zotero", "-v", "mcp-data:/data", "mcp-local-reference"]` |

## Docker

| File | Purpose |
|------|---------|
| `Dockerfile` | Single-stage build on `python:3.11-slim` |
| `.dockerignore` | Excludes .git, tests, .venv, docs |

### Build

| Step | Command |
|------|---------|
| Build image | `docker build -t mcp-local-reference .` |
| Run (stdio) | `docker run -i --rm -v ~/Zotero:/zotero mcp-local-reference` |

### Volume Mounts

| Host Path | Container Path | Purpose |
|-----------|---------------|---------|
| `~/Zotero` (or custom) | `/zotero` | Zotero data directory (read-only) |
| Named volume `mcp-data` | `/data` | ChromaDB vector index (persistent) |

## CI/CD (GitHub Actions)

| File | Trigger | Jobs |
|------|---------|------|
| `.github/workflows/ci.yml` | push to main, PRs | lint, test |

### Lint Job

| Step | Command |
|------|---------|
| Check | `ruff check src/ tests/` |
| Format | `ruff format --check src/ tests/` |

### Test Job

| Step | Command |
|------|---------|
| Install | `uv pip install --system -e ".[dev]"` |
| Run | `pytest -v` |

## Environment Variables

| Variable | Default | Description | Used by |
|----------|---------|-------------|---------|
| `ZOTERO_DATA_DIR` | `~/Zotero` | Zotero data directory | `config.py` |
| `MCP_DATA_DIR` | `~/.mcp-local-reference` | Server data (vector index) | `config.py` |
| `FIGURE_DPI` | `300` | Rendering DPI for cropped figures | `config.py` |
| `MIN_FIGURE_PIXELS` | `10000` | Minimum pixel area for figure detection | `config.py` |

---

<!-- Maintenance: Update when Docker config, CI pipeline, or env vars change -->
