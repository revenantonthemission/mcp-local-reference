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
    _register_create_collection(mcp, api, zotero)
    _register_rename_collection(mcp, api, zotero)
    _ = pdf  # placeholder until suggest_collection_placement is added in Task 15


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


# ----------------------------------------------------------------------
# create_collection
# ----------------------------------------------------------------------


def _register_create_collection(mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient) -> None:
    @mcp.tool()
    def create_collection(name: str, parent_key: str | None = None, dry_run: bool = True) -> str:
        """Create a new Zotero collection (folder).

        Defaults to dry_run=True. If a collection with the same (name,
        parent_key) already exists, returns status='already_exists' with
        the existing key — no duplicate is created.

        Args:
            name: The collection name. Must be non-empty.
            parent_key: Parent collection key, or None to create at the
                library root.
            dry_run: If True (default), report the preview without writing.
        """
        return create_collection_impl(api, zotero, name, parent_key, dry_run)


def create_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    name: str,
    parent_key: str | None,
    dry_run: bool,
) -> str:
    name = (name or "").strip()
    if not name:
        return _err("No collection name provided")

    all_cols = _flatten_tree(zotero.list_collections())
    by_key = {c.key: c for c in all_cols}

    if parent_key is not None and parent_key not in by_key:
        return _err(f"Parent collection '{parent_key}' not found")

    existing = next(
        (c for c in all_cols if c.name == name and c.parent_key == parent_key),
        None,
    )
    if existing is not None:
        return json.dumps(
            {
                "status": "already_exists",
                "existing_key": existing.key,
                "name": name,
                "parent_key": parent_key,
                "dry_run": dry_run,
            }
        )

    if dry_run:
        return json.dumps(
            {
                "status": "preview",
                "would_create": {"name": name, "parent_key": parent_key},
                "dry_run": True,
            }
        )

    try:
        snap = api.create_collection(name, parent_key)
    except (MissingCredentialsError, ZoteroApiError) as exc:
        return _err(str(exc))

    return json.dumps(
        {
            "status": "applied",
            "created": {
                "collection_key": snap.collection_key,
                "name": snap.name,
                "parent_key": snap.parent_key,
            },
            "new_version": snap.version,
            "dry_run": False,
        }
    )


# ----------------------------------------------------------------------
# rename_collection
# ----------------------------------------------------------------------


def _register_rename_collection(mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient) -> None:
    @mcp.tool()
    def rename_collection(collection_key: str, new_name: str, dry_run: bool = True) -> str:
        """Rename an existing Zotero collection.

        Defaults to dry_run=True. Refuses sibling-name collisions
        (a collection with the same new_name already under the same
        parent) for hygiene; Zotero itself permits this.
        """
        return rename_collection_impl(api, zotero, collection_key, new_name, dry_run)


def rename_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    new_name: str,
    dry_run: bool,
) -> str:
    new_name = (new_name or "").strip()
    if not new_name:
        return _err("No new name provided")

    local = _local_collection_snapshot(zotero, collection_key)
    if local is None:
        return _err(f"Reference '{collection_key}' not found")

    if local.name == new_name:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "status": "no_changes",
                "dry_run": dry_run,
            }
        )

    siblings = [
        c
        for c in _flatten_tree(zotero.list_collections())
        if c.parent_key == local.parent_key and c.key != collection_key
    ]
    if any(c.name == new_name for c in siblings):
        return _err(f"A collection named '{new_name}' already exists under this parent")

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "would_rename_to": new_name,
                "after": {"name": new_name, "parent_key": local.parent_key},
                "status": "preview",
                "dry_run": True,
            }
        )

    try:
        snap = api.get_collection(collection_key)
        new_version = api.update_collection(collection_key, name=new_name, version=snap.version)
    except (MissingCredentialsError, VersionConflictError, ZoteroApiError) as exc:
        return _err(str(exc))

    return json.dumps(
        {
            "collection_key": collection_key,
            "current": {"name": local.name, "parent_key": local.parent_key},
            "after": {"name": new_name, "parent_key": local.parent_key},
            "new_version": new_version,
            "status": "applied",
            "dry_run": False,
        }
    )
