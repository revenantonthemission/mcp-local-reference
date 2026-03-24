FROM python:3.11-slim AS base

WORKDIR /app

# System deps for PyMuPDF (needs some C libs)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libmupdf-dev && \
    rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

# Copy project metadata first (layer caching)
COPY pyproject.toml ./

# Copy source
COPY src/ src/

# Install the package
RUN uv pip install --system .

# Persistent data directory for ChromaDB index
RUN mkdir -p /data/chroma
ENV MCP_DATA_DIR=/data
ENV ZOTERO_DATA_DIR=/zotero

# The Zotero data directory must be mounted at runtime:
#   docker run -v ~/Zotero:/zotero ...
ENTRYPOINT ["mcp-local-reference"]
