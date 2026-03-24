"""MCP tools for searching, retrieving, and citing Zotero references."""

from __future__ import annotations

import json
import re

from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.services.vector_store import VectorStore
from mcp_local_reference.services.zotero_client import Reference, ZoteroClient


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register all reference-related tools on *mcp*."""
    zotero = ZoteroClient(config)
    vector_store = VectorStore(config)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def search_references(
        query: str,
        limit: int = 10,
        semantic: bool = True,
    ) -> str:
        """Search your Zotero library for references.

        Uses semantic (vector-similarity) search when the library has been
        indexed with ``index_library``.  Falls back to keyword search
        otherwise.

        Args:
            query: Search query — keywords or a natural-language description.
            limit: Maximum number of results (default 10).
            semantic: Prefer semantic search when an index exists (default True).
        """
        if semantic and vector_store.is_indexed():
            results = vector_store.search(query, limit)
            enriched = []
            for r in results:
                ref = zotero.get_reference(r["item_key"])
                if ref:
                    enriched.append({**_summary(ref), "relevance_score": r["relevance_score"]})
            return json.dumps(enriched, indent=2, ensure_ascii=False)

        refs = zotero.search(query, limit)
        return json.dumps([_summary(r) for r in refs], indent=2, ensure_ascii=False)

    @mcp.tool()
    def get_reference(item_key: str) -> str:
        """Get full metadata for a Zotero reference.

        Args:
            item_key: The 8-character Zotero item key (shown in search results).
        """
        ref = zotero.get_reference(item_key)
        if ref is None:
            return json.dumps({"error": f"Reference '{item_key}' not found"})
        return json.dumps(ref.to_dict(), indent=2, ensure_ascii=False)

    @mcp.tool()
    def list_collections() -> str:
        """List every Zotero collection (folder) as a nested tree."""
        collections = zotero.list_collections()
        return json.dumps(
            [_collection_to_dict(c) for c in collections],
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool()
    def get_collection_items(collection_key: str, limit: int = 50) -> str:
        """Get references inside a Zotero collection.

        Args:
            collection_key: The collection key (shown in list_collections output).
            limit: Maximum items to return (default 50).
        """
        refs = zotero.get_collection_items(collection_key, limit)
        return json.dumps([_summary(r) for r in refs], indent=2, ensure_ascii=False)

    @mcp.tool()
    def format_citation(item_key: str) -> str:
        """Format a reference as a Harvard (Cite Them Right) citation.

        Args:
            item_key: The Zotero item key.
        """
        ref = zotero.get_reference(item_key)
        if ref is None:
            return f"Error: Reference '{item_key}' not found"
        return _format_harvard_ctr(ref)

    @mcp.tool()
    def index_library() -> str:
        """Index your Zotero library into a vector store for semantic search.

        Reads all references, embeds title + abstract + tags + authors, and
        stores them in a local ChromaDB database.  Run this once after setup,
        or again whenever you add new references to Zotero.
        """
        refs = zotero.get_all_references()
        count = vector_store.index_references([r.to_dict() for r in refs])
        return json.dumps(
            {
                "status": "success",
                "indexed": count,
                "message": f"Indexed {count} references for semantic search.",
            }
        )


# ======================================================================
# Private helpers
# ======================================================================


def _summary(ref: Reference) -> dict[str, str]:
    return {
        "item_key": ref.item_key,
        "title": ref.title,
        "authors": _authors_short(ref.creators),
        "year": _extract_year(ref.date),
        "type": ref.item_type,
    }


def _authors_short(creators: list[dict[str, str]]) -> str:
    authors = [c for c in creators if c.get("creatorType") == "author"] or creators
    if not authors:
        return "Unknown"
    names = [c.get("lastName", c.get("firstName", "")) for c in authors]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


def _extract_year(date_str: str) -> str:
    if not date_str:
        return "n.d."
    m = re.search(r"\d{4}", date_str)
    return m.group() if m else date_str


# ------------------------------------------------------------------
# Harvard Cite Them Right formatting
# ------------------------------------------------------------------


def _format_harvard_ctr(ref: Reference) -> str:
    """Return a Harvard Cite Them Right reference-list entry."""
    authors = _harvard_authors(ref.creators)
    year = _extract_year(ref.date)

    formatters = {
        "journalArticle": _fmt_journal,
        "book": _fmt_book,
        "document": _fmt_book,
        "bookSection": _fmt_book_section,
        "conferencePaper": _fmt_conference,
        "thesis": _fmt_thesis,
        "webpage": _fmt_webpage,
        "report": _fmt_report,
    }
    formatter = formatters.get(ref.item_type, _fmt_generic)
    return formatter(ref, authors, year)


def _fmt_journal(ref: Reference, authors: str, year: str) -> str:
    parts = [f"{authors} ({year})"]
    if ref.title:
        parts.append(f"'{ref.title}',")
    if ref.publication:
        parts.append(f"*{ref.publication}*,")
    if ref.volume:
        vi = ref.volume
        if ref.issue:
            vi += f"({ref.issue})"
        parts.append(f"{vi},")
    if ref.pages:
        parts.append(f"pp. {ref.pages}.")
    if ref.doi:
        parts.append(f"doi:{ref.doi}.")
    return " ".join(parts)


def _fmt_book(ref: Reference, authors: str, year: str) -> str:
    parts = [f"{authors} ({year})"]
    if ref.title:
        parts.append(f"*{ref.title}*.")
    if ref.edition:
        parts.append(f"{ref.edition}.")
    if ref.place and ref.publisher:
        parts.append(f"{ref.place}: {ref.publisher}.")
    elif ref.publisher:
        parts.append(f"{ref.publisher}.")
    return " ".join(parts)


def _fmt_book_section(ref: Reference, authors: str, year: str) -> str:
    editors = _harvard_editors(ref.creators)
    parts = [f"{authors} ({year})"]
    if ref.title:
        parts.append(f"'{ref.title}',")
    if editors:
        parts.append(f"in {editors}")
    if ref.publication:
        parts.append(f"*{ref.publication}*.")
    if ref.place and ref.publisher:
        parts.append(f"{ref.place}: {ref.publisher},")
    if ref.pages:
        parts.append(f"pp. {ref.pages}.")
    return " ".join(parts)


def _fmt_conference(ref: Reference, authors: str, year: str) -> str:
    parts = [f"{authors} ({year})"]
    if ref.title:
        parts.append(f"'{ref.title}',")
    if ref.publication:
        parts.append(f"*{ref.publication}*.")
    if ref.place:
        parts.append(f"{ref.place}.")
    if ref.pages:
        parts.append(f"pp. {ref.pages}.")
    if ref.doi:
        parts.append(f"doi:{ref.doi}.")
    return " ".join(parts)


def _fmt_thesis(ref: Reference, authors: str, year: str) -> str:
    parts = [f"{authors} ({year})"]
    if ref.title:
        parts.append(f"*{ref.title}*.")
    parts.append("Thesis.")
    if ref.publisher:
        parts.append(f"{ref.publisher}.")
    return " ".join(parts)


def _fmt_webpage(ref: Reference, authors: str, year: str) -> str:
    parts = [f"{authors} ({year})"]
    if ref.title:
        parts.append(f"*{ref.title}*.")
    if ref.url:
        parts.append(f"Available at: {ref.url}.")
    return " ".join(parts)


def _fmt_report(ref: Reference, authors: str, year: str) -> str:
    parts = [f"{authors} ({year})"]
    if ref.title:
        parts.append(f"*{ref.title}*.")
    if ref.publisher:
        parts.append(f"{ref.publisher}.")
    if ref.url:
        parts.append(f"Available at: {ref.url}.")
    return " ".join(parts)


def _fmt_generic(ref: Reference, authors: str, year: str) -> str:
    parts = [f"{authors} ({year})"]
    if ref.title:
        parts.append(f"'{ref.title}'.")
    if ref.publisher:
        parts.append(f"{ref.publisher}.")
    if ref.doi:
        parts.append(f"doi:{ref.doi}.")
    return " ".join(parts)


# ------------------------------------------------------------------
# Author / editor formatting
# ------------------------------------------------------------------


def _harvard_authors(creators: list[dict[str, str]]) -> str:
    """Format authors as ``LastName, F.F.`` per Harvard CTR rules."""
    authors = [c for c in creators if c.get("creatorType") == "author"] or creators
    if not authors:
        return "Unknown"

    def _one(c: dict[str, str]) -> str:
        last = c.get("lastName", "")
        first = c.get("firstName", "")
        if first:
            initials = ".".join(w[0].upper() for w in first.split() if w) + "."
            return f"{last}, {initials}"
        return last

    if len(authors) == 1:
        return _one(authors[0])
    if len(authors) == 2:
        return f"{_one(authors[0])} and {_one(authors[1])}"
    if len(authors) == 3:
        return f"{_one(authors[0])}, {_one(authors[1])} and {_one(authors[2])}"
    return f"{_one(authors[0])} et al."


def _harvard_editors(creators: list[dict[str, str]]) -> str:
    editors = [c for c in creators if c.get("creatorType") == "editor"]
    if not editors:
        return ""

    def _one(c: dict[str, str]) -> str:
        last = c.get("lastName", "")
        first = c.get("firstName", "")
        if first:
            initials = ".".join(w[0].upper() for w in first.split() if w) + "."
            return f"{last}, {initials}"
        return last

    if len(editors) == 1:
        return f"{_one(editors[0])} (ed.)"
    names = [_one(e) for e in editors]
    return f"{', '.join(names[:-1])} and {names[-1]} (eds.)"


def _collection_to_dict(col: object) -> dict[str, object]:
    return {
        "key": col.key,  # type: ignore[attr-defined]
        "name": col.name,  # type: ignore[attr-defined]
        "children": [_collection_to_dict(c) for c in col.children],  # type: ignore[attr-defined]
    }
