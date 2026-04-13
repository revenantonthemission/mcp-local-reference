"""Code MCP server — standalone MCP server for source code search."""

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

if TYPE_CHECKING:
    from .code_manager import CodeIndexManager

# MCP server instance
server = Server("code-mcp")

# Shared CodeIndexManager singleton (lazy-initialized)
_manager: "CodeIndexManager | None" = None


def get_manager() -> "CodeIndexManager":
    """Return the shared CodeIndexManager singleton, creating it if needed."""
    global _manager
    if _manager is None:
        from .code_manager import CodeIndexManager

        _manager = CodeIndexManager()
    return _manager


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="search_code",
            description="Search indexed source code repositories for functions, classes, "
            "and symbols. Use this to find code examples and implementations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (function names, keywords, concepts)",
                    },
                    "search_type": {
                        "type": "string",
                        "enum": ["hybrid", "keyword", "semantic"],
                        "default": "hybrid",
                        "description": (
                            "Search method: hybrid (default), keyword (FTS), or semantic (vector)"
                        ),
                    },
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by repository names (e.g. ['linux', 'cpython'])",
                    },
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by languages (e.g. ['python', 'c', 'go'])",
                    },
                    "symbol_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by symbol types (e.g. ['function', 'class'])",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Maximum number of results",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_repos",
            description="List all indexed source code repositories with file and symbol counts.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_index_status",
            description="Get statistics about the code search index.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_symbol",
            description="Browse symbols in an indexed file. Without a name, lists all symbols "
            "(overview). With a name, returns the full source of that symbol.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name (e.g. 'linux', 'cpython')",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the repo (e.g. 'src/main.py')",
                    },
                    "name": {
                        "type": "string",
                        "description": "Symbol name to retrieve (omit for file overview)",
                    },
                },
                "required": ["repo", "path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    handlers = {
        "search_code": handle_search_code,
        "list_repos": handle_list_repos,
        "get_index_status": handle_get_index_status,
        "get_symbol": handle_get_symbol,
    }

    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    try:
        result = await handler(**arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        logging.exception(f"Tool {name} failed: {e}")
        return [
            TextContent(
                type="text",
                text=f"Error: Tool '{name}' failed - {e}",
            )
        ]


# --- Tool handlers ---


async def handle_search_code(
    query: str,
    search_type: str = "hybrid",
    repos: list[str] | None = None,
    languages: list[str] | None = None,
    symbol_types: list[str] | None = None,
    limit: int = 20,
) -> str:
    """Search indexed source code repositories."""
    if not query.strip():
        return "Error: Search query cannot be empty."

    limit = min(limit, 100)
    manager = get_manager()

    logger = logging.getLogger(__name__)
    logger.info(f"Performing {search_type} code search for: {query}")

    if search_type == "keyword":
        results = manager.keyword_search(query, limit, repos, languages)
    elif search_type == "semantic":
        results = manager.semantic_search(query, limit, repos, languages)
    else:  # hybrid
        results = await manager.hybrid_search(query, limit, repos, languages)

    # Post-search filter by symbol type.
    # Uses both exact match and substring match because tree-sitter node types
    # vary by language (e.g. "function_definition" vs "function_declaration"),
    # so a user filter of "function" should match both.
    if symbol_types:
        type_set = {t.lower() for t in symbol_types}
        results = [
            r
            for r in results
            if r.get("symbol_type", "").lower() in type_set
            or any(t in r.get("symbol_type", "").lower() for t in type_set)
        ]
        results = results[:limit]

    return _format_code_results(query, results, search_type, limit)


async def handle_list_repos() -> str:
    """List all indexed repositories."""
    manager = get_manager()
    repos = manager.list_repos()

    if not repos:
        return (
            "## Indexed Repositories\n\n"
            "No repositories indexed yet.\n"
            "Run `code-mcp-index` to index your source code repositories."
        )

    lines = ["## Indexed Repositories", ""]
    for r in repos:
        lines.append(
            f"- **{r['name']}** — {r.get('file_count', 0)} files, "
            f"{r.get('symbol_count', 0)} symbols "
            f"(indexed: {r.get('indexed_at', 'unknown')})"
        )

    lines.append("")
    lines.append(f"**Total**: {len(repos)} repositories")
    return "\n".join(lines)


async def handle_get_index_status() -> str:
    """Get code index statistics."""
    manager = get_manager()
    stats = manager.get_stats()

    return f"""## Code Index Status

| Metric | Value |
|--------|-------|
| Repositories | {stats.get("repo_count", 0)} |
| Files | {stats.get("file_count", 0)} |
| Symbols | {stats.get("symbol_count", 0)} |
| FTS Index Size | {stats.get("index_size_mb", 0):.1f} MB |
| Vector Index Size | {stats.get("vector_index_size_mb", 0):.1f} MB |
| Total Size | {stats.get("total_index_size_mb", 0):.1f} MB |
"""


async def handle_get_symbol(
    repo: str,
    path: str,
    name: str | None = None,
) -> str:
    """Get symbol details or file overview."""
    manager = get_manager()

    if name:
        # Specific symbol mode — returns list (may have multiple matches)
        matches = manager.fts_index.get_symbol_by_name(repo, path, name)
        if not matches:
            return (
                f"Symbol `{name}` not found in `{repo}:{path}`.\n\n"
                "Use `get_symbol` without a name to see available symbols."
            )

        parts = []
        for symbol in matches:
            parent = symbol.get("parent_name", "")
            display_name = f"{parent}.{symbol['symbol_name']}" if parent else symbol["symbol_name"]
            lang = symbol.get("language", "")
            parts.append(
                f"## `{display_name}` ({symbol['symbol_type']})\n\n"
                f"**File**: `{repo}:{path}` | "
                f"**Lines**: {symbol['start_line']}-{symbol['end_line']} | "
                f"**Language**: {lang}\n\n"
                f"```{lang}\n{symbol['text']}\n```"
            )
        return "\n\n---\n\n".join(parts)

    # Overview mode — list all symbols in the file
    symbols = manager.fts_index.get_symbols_by_file(repo, path)
    if not symbols:
        return (
            f"No symbols found in `{repo}:{path}`.\n\n"
            "The file may not be indexed. Check `list_repos` or re-index."
        )

    lines = [f"## Symbols in `{repo}:{path}`", ""]
    for s in symbols:
        parent = s.get("parent_name", "")
        display_name = f"{parent}.{s['symbol_name']}" if parent else s["symbol_name"]
        lines.append(
            f"- **`{display_name}`** ({s['symbol_type']}) — lines {s['start_line']}-{s['end_line']}"
        )

    lines.append("")
    lines.append(f"**Total**: {len(symbols)} symbols")
    lines.append("")
    lines.append("Use `get_symbol` with `name` parameter to see full source.")
    return "\n".join(lines)


# --- Formatting ---


def _format_code_results(
    query: str,
    results: list[dict],
    search_type: str,
    limit: int,
) -> str:
    """Format code search results as markdown."""
    if not results:
        return f"""## Code Search Results

**Query**: "{query}"
**Search Type**: {search_type}

No results found. Try:
- Using different keywords or function/class names
- Using semantic search for conceptual queries
- Checking if repositories have been indexed (`code-mcp-index`)
"""

    # Compute max score for normalization
    all_scores = [r.get("score", 0) for r in results if r.get("score") is not None]
    max_score = max(all_scores) if all_scores else 1.0
    if max_score <= 0:
        max_score = 1.0

    output = [
        "## Code Search Results",
        "",
        f'**Query**: "{query}"',
        f"**Search Type**: {search_type}",
        f"**Found**: {len(results)} matching symbols",
        "",
    ]

    for i, r in enumerate(results[:limit], 1):
        repo = r.get("repo_name", "unknown")
        rel_path = r.get("rel_path", "")
        name = r.get("symbol_name", "")
        sym_type = r.get("symbol_type", "")
        language = r.get("language", "")
        start = r.get("start_line", 0)
        end = r.get("end_line", 0)
        text = r.get("text", "")

        score = r.get("score", 0) or 0
        relevance_pct = int(round(score / max_score * 100))

        # Header (show parent.name if nested)
        parent = r.get("parent_name", "")
        display_name = f"{parent}.{name}" if parent else name
        output.append(f"### {i}. `{display_name}` ({sym_type})")
        output.append(
            f"**Repo**: {repo} | **File**: `{rel_path}`"
            f" | **Lines**: {start}-{end}"
            f" | **Language**: {language}"
            f" | **Relevance**: {relevance_pct}%"
        )
        output.append("")

        # Show highlights if available (keyword search)
        highlights = r.get("highlights")
        if highlights:
            highlighted = highlights.replace("<mark>", "**").replace("</mark>", "**")
            if len(highlighted) > 500:
                highlighted = highlighted[:500] + "..."
            output.append(f"> {highlighted}")
            output.append("")

        # Show code block (truncated)
        if text:
            display_text = text if len(text) <= 800 else text[:800] + "\n// ... (truncated)"
            output.append(f"```{language}")
            output.append(display_text)
            output.append("```")
            output.append("")

    output.append("---")
    output.append(
        "**Tip**: Use `search_code` with `repos` or `languages` filters to narrow results."
    )

    return "\n".join(output)


def main():
    """Run the code MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(run_server())


async def run_server():
    """Run the async server."""
    from .config import settings

    logger = logging.getLogger(__name__)

    logger.info("Initializing code index manager...")
    manager = get_manager()
    logger.info("Code index manager ready")

    # Start file watcher if enabled
    watcher = None
    if settings.watch_enabled:
        try:
            from .code_watcher import CodeWatcher

            watcher = CodeWatcher(manager)
            watcher.start()
            logger.info("Code file watcher started")
        except Exception as e:
            logger.warning(f"Failed to start file watcher: {e}")

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if watcher:
            watcher.stop()
            logger.info("Code file watcher stopped")


if __name__ == "__main__":
    main()
