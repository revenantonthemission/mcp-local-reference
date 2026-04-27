"""Collection-editing MCP tools.

Seven tools mirror the auto_tag.py pattern but operate on Zotero
collections instead of tags. Six write tools default to dry_run=True;
the seventh (suggest_collection_placement) is read-only and gathers
context for Claude to advise on placement without writing.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.services.pdf_processor import PdfProcessor
from mcp_local_reference.services.zotero_api_client import (
    CollectionSnapshot,
    ItemSnapshot,  # noqa: F401 — used by future tool implementations
    MissingCredentialsError,  # noqa: F401 — used by future tool implementations
    VersionConflictError,  # noqa: F401 — used by future tool implementations
    ZoteroApiClient,
    ZoteroApiError,  # noqa: F401 — used by future tool implementations
)
from mcp_local_reference.services.zotero_client import Collection, ZoteroClient

MAX_ITEMS_PER_CALL = 25


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register collection-editing tools on *mcp*."""
    zotero = ZoteroClient(config)
    api = ZoteroApiClient(config)
    pdf = PdfProcessor(min_figure_pixels=config.min_figure_pixels)

    # Tool registration is added in Tasks 9–15. Each registers one tool.
    # The api/zotero/pdf instances above are passed into each `_register_*`.
    _ = (api, zotero, pdf)  # placeholder until tools are wired in


# ----------------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------------


def _local_collection_snapshot(
    zotero: ZoteroClient, collection_key: str
) -> CollectionSnapshot | None:
    """Build a CollectionSnapshot from local SQLite, or return None if missing.

    Mirrors `_local_snapshot` in auto_tag.py: version=0 is intentionally
    invalid so a local snapshot accidentally fed to a Web API write
    fails loudly with HTTP 412 instead of silently corrupting.
    """
    for col in _flatten_tree(zotero.list_collections()):
        if col.key == collection_key:
            return CollectionSnapshot(
                collection_key=col.key,
                version=0,
                name=col.name,
                parent_key=col.parent_key,
                raw={},
            )
    return None


def _flatten_tree(roots: list[Collection]) -> list[Collection]:
    """Walk the nested collection tree and return every node as a flat list."""
    out: list[Collection] = []
    stack: list[Collection] = list(roots)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


def _walk_descendants(zotero: ZoteroClient, root_key: str) -> set[str]:
    """Return the keys of every descendant of root_key (excluding root_key itself)."""
    all_cols = _flatten_tree(zotero.list_collections())
    children_of: dict[str, list[str]] = {}
    for c in all_cols:
        if c.parent_key:
            children_of.setdefault(c.parent_key, []).append(c.key)
    descendants: set[str] = set()
    stack: list[str] = list(children_of.get(root_key, []))
    while stack:
        node = stack.pop()
        if node in descendants:
            continue
        descendants.add(node)
        stack.extend(children_of.get(node, []))
    return descendants


def _check_cycle(zotero: ZoteroClient, target_key: str, new_parent_key: str) -> bool:
    """Return True if reparenting `target_key` under `new_parent_key` would cycle."""
    if target_key == new_parent_key:
        return True
    return new_parent_key in _walk_descendants(zotero, target_key)


def _err(message: str) -> str:
    return json.dumps({"error": message})
