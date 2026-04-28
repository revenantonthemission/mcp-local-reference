"""Collection-editing MCP tools.

Seven tools mirror the auto_tag.py pattern but operate on Zotero
collections instead of tags. Six write tools default to dry_run=True;
the seventh (suggest_collection_placement) is read-only and gathers
context for Claude to advise on placement without writing.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.services.pdf_processor import PdfProcessor
from mcp_local_reference.services.zotero_api_client import (
    CollectionSnapshot,
    MissingCredentialsError,
    VersionConflictError,
    ZoteroApiClient,
    ZoteroApiError,
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
    _register_reparent_collection(mcp, api, zotero)
    _register_delete_collection(mcp, api, zotero)
    _register_add_items_to_collection(mcp, api, zotero)
    _register_remove_items_from_collection(mcp, api, zotero)
    _register_suggest_collection_placement(mcp, zotero, pdf)


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


# ----------------------------------------------------------------------
# reparent_collection
# ----------------------------------------------------------------------


def _register_reparent_collection(mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient) -> None:
    @mcp.tool()
    def reparent_collection(
        collection_key: str,
        new_parent_key: str | None,
        dry_run: bool = True,
    ) -> str:
        """Move a collection under a different parent.

        Pass new_parent_key=None to move to the library root.
        Refuses cycles (re-parenting a collection under itself or one
        of its descendants).
        """
        return reparent_collection_impl(api, zotero, collection_key, new_parent_key, dry_run)


def reparent_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    new_parent_key: str | None,
    dry_run: bool,
) -> str:
    local = _local_collection_snapshot(zotero, collection_key)
    if local is None:
        return _err(f"Reference '{collection_key}' not found")

    if new_parent_key is not None:
        all_keys = {c.key for c in _flatten_tree(zotero.list_collections())}
        if new_parent_key not in all_keys:
            return _err(f"Parent collection '{new_parent_key}' not found")
        if _check_cycle(zotero, collection_key, new_parent_key):
            return _err(
                f"Cycle detected: collection {collection_key} cannot be re-parented "
                f"under its own descendant {new_parent_key}"
            )

    if local.parent_key == new_parent_key:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "status": "no_changes",
                "dry_run": dry_run,
            }
        )

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "would_reparent_to": new_parent_key,
                "after": {"name": local.name, "parent_key": new_parent_key},
                "status": "preview",
                "dry_run": True,
            }
        )

    try:
        snap = api.get_collection(collection_key)
        new_version = api.update_collection(
            collection_key, parent_key=new_parent_key, version=snap.version
        )
    except (MissingCredentialsError, VersionConflictError, ZoteroApiError) as exc:
        return _err(str(exc))

    return json.dumps(
        {
            "collection_key": collection_key,
            "current": {"name": local.name, "parent_key": local.parent_key},
            "after": {"name": local.name, "parent_key": new_parent_key},
            "new_version": new_version,
            "status": "applied",
            "dry_run": False,
        }
    )


# ----------------------------------------------------------------------
# delete_collection
# ----------------------------------------------------------------------


def _register_delete_collection(mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient) -> None:
    @mcp.tool()
    def delete_collection(collection_key: str, dry_run: bool = True) -> str:
        """Delete a collection. Items inside are NOT deleted — they lose
        membership in this collection. Sub-collections become orphans.
        Dry-run lists both blast surfaces before any write.
        """
        return delete_collection_impl(api, zotero, collection_key, dry_run)


def delete_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    dry_run: bool,
) -> str:
    local = _local_collection_snapshot(zotero, collection_key)
    if local is None:
        return _err(f"Reference '{collection_key}' not found")

    items_inside = [r.item_key for r in zotero.get_collection_items(collection_key, limit=10_000)]
    descendants = sorted(_walk_descendants(zotero, collection_key))

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "would_orphan_items": items_inside,
                "would_orphan_collections": descendants,
                "status": "preview",
                "dry_run": True,
            }
        )

    try:
        snap = api.get_collection(collection_key)
        api.delete_collection(collection_key, snap.version)
    except (MissingCredentialsError, VersionConflictError, ZoteroApiError) as exc:
        return _err(str(exc))

    return json.dumps(
        {
            "collection_key": collection_key,
            "deleted": {"name": local.name, "parent_key": local.parent_key},
            "would_orphan_items": items_inside,
            "would_orphan_collections": descendants,
            "status": "applied",
            "dry_run": False,
        }
    )


# ----------------------------------------------------------------------
# add_items_to_collection
# ----------------------------------------------------------------------


def _register_add_items_to_collection(
    mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient
) -> None:
    @mcp.tool()
    def add_items_to_collection(
        collection_key: str,
        item_keys: list[str],
        dry_run: bool = True,
    ) -> str:
        """Add items to a Zotero collection (set-union per item).

        Defaults to dry_run=True. Per-call cap of 25 items. Each item
        is patched independently — partial failures are reported in the
        `failed` list while successful items are committed.
        """
        return add_items_to_collection_impl(api, zotero, collection_key, item_keys, dry_run)


def add_items_to_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    item_keys: list[str],
    dry_run: bool,
) -> str:
    cleaned = [k.strip() for k in item_keys if k and k.strip()]
    if not cleaned:
        return _err("No item keys provided")
    if len(cleaned) > MAX_ITEMS_PER_CALL:
        return _err(
            f"Refusing: {len(cleaned)} items exceeds the per-call cap of "
            f"{MAX_ITEMS_PER_CALL}. Split into smaller batches."
        )

    coll_snap = _local_collection_snapshot(zotero, collection_key)
    if coll_snap is None:
        return _err(f"Reference '{collection_key}' not found")

    would_add: list[str] = []
    already_present: list[str] = []
    not_found: list[str] = []
    for k in cleaned:
        if zotero.get_reference(k) is None:
            not_found.append(k)
            continue
        current = zotero.get_item_collections(k)
        if collection_key in current:
            already_present.append(k)
        else:
            would_add.append(k)

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "would_add": would_add,
                "already_present": already_present,
                "not_found": not_found,
                "status": "preview",
                "dry_run": True,
            }
        )

    if not would_add:
        return json.dumps(
            {
                "collection_key": collection_key,
                "succeeded": [],
                "failed": [],
                "already_present": already_present,
                "not_found": not_found,
                "status": "no_changes",
                "dry_run": False,
            }
        )

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item_key in would_add:
        try:
            snap = api.get_item(item_key)
            new_colls = sorted({*snap.collections, collection_key})
            new_version = api.update_item_collections(item_key, new_colls, snap.version)
            succeeded.append({"item_key": item_key, "new_version": new_version})
        except MissingCredentialsError as exc:
            return _err(str(exc))
        except (VersionConflictError, ZoteroApiError) as exc:
            failed.append({"item_key": item_key, "reason": str(exc)})

    return json.dumps(
        {
            "collection_key": collection_key,
            "succeeded": succeeded,
            "failed": failed,
            "already_present": already_present,
            "not_found": not_found,
            "status": "partial" if failed else "applied",
            "dry_run": False,
        }
    )


def _register_remove_items_from_collection(
    mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient
) -> None:
    @mcp.tool()
    def remove_items_from_collection(
        collection_key: str,
        item_keys: list[str],
        dry_run: bool = True,
    ) -> str:
        """Remove items from a collection (set-difference per item).

        Mirror of add_items_to_collection. Items not currently in the
        collection are reported under `not_present` and silently skipped.
        Cap of 25, partial-failure semantics.
        """
        return remove_items_from_collection_impl(api, zotero, collection_key, item_keys, dry_run)


def remove_items_from_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    item_keys: list[str],
    dry_run: bool,
) -> str:
    cleaned = [k.strip() for k in item_keys if k and k.strip()]
    if not cleaned:
        return _err("No item keys provided")
    if len(cleaned) > MAX_ITEMS_PER_CALL:
        return _err(
            f"Refusing: {len(cleaned)} items exceeds the per-call cap of "
            f"{MAX_ITEMS_PER_CALL}. Split into smaller batches."
        )

    coll_snap = _local_collection_snapshot(zotero, collection_key)
    if coll_snap is None:
        return _err(f"Reference '{collection_key}' not found")

    would_remove: list[str] = []
    not_present: list[str] = []
    not_found: list[str] = []
    for k in cleaned:
        if zotero.get_reference(k) is None:
            not_found.append(k)
            continue
        current = zotero.get_item_collections(k)
        if collection_key in current:
            would_remove.append(k)
        else:
            not_present.append(k)

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "would_remove": would_remove,
                "not_present": not_present,
                "not_found": not_found,
                "status": "preview",
                "dry_run": True,
            }
        )

    if not would_remove:
        return json.dumps(
            {
                "collection_key": collection_key,
                "succeeded": [],
                "failed": [],
                "not_present": not_present,
                "not_found": not_found,
                "status": "no_changes",
                "dry_run": False,
            }
        )

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item_key in would_remove:
        try:
            snap = api.get_item(item_key)
            new_colls = [c for c in snap.collections if c != collection_key]
            new_version = api.update_item_collections(item_key, new_colls, snap.version)
            succeeded.append({"item_key": item_key, "new_version": new_version})
        except MissingCredentialsError as exc:
            return _err(str(exc))
        except (VersionConflictError, ZoteroApiError) as exc:
            failed.append({"item_key": item_key, "reason": str(exc)})

    return json.dumps(
        {
            "collection_key": collection_key,
            "succeeded": succeeded,
            "failed": failed,
            "not_present": not_present,
            "not_found": not_found,
            "status": "partial" if failed else "applied",
            "dry_run": False,
        }
    )


# ----------------------------------------------------------------------
# suggest_collection_placement
# ----------------------------------------------------------------------


def _register_suggest_collection_placement(
    mcp: FastMCP, zotero: ZoteroClient, pdf: PdfProcessor
) -> None:
    @mcp.tool()
    def suggest_collection_placement(item_key: str) -> str:
        """Gather context for advising where an item should be filed.

        Returns the item's title and abstract (or first-page PDF snippet
        when the abstract is empty), the full collection tree, the
        item's current memberships, and a per-collection item count so
        well-populated folders can be preferred over thinly-used ones.
        Read-only — no writes.
        """
        return suggest_collection_placement_impl(zotero, pdf, item_key)


def suggest_collection_placement_impl(
    zotero: ZoteroClient,
    pdf: PdfProcessor,
    item_key: str,
) -> str:
    ref = zotero.get_reference(item_key)
    if ref is None:
        return _err(f"Reference '{item_key}' not found")

    abstract = (ref.abstract or "").strip()
    if not abstract:
        try:
            pdf_path = zotero.get_pdf_path(item_key)
        except Exception:  # noqa: BLE001 — local helper, never crash the tool
            pdf_path = None
        if pdf_path is not None:
            try:
                snippet = pdf.extract_text(pdf_path, start_page=0, end_page=1)
                if snippet:
                    abstract = snippet[:2000]
            except Exception:  # noqa: BLE001
                pass

    all_cols = _flatten_tree(zotero.list_collections())
    by_key = {c.key: c for c in all_cols}
    current_keys = zotero.get_item_collections(item_key)
    current_collections = [{"key": k, "name": by_key[k].name} for k in current_keys if k in by_key]

    vocabulary: dict[str, int] = {}
    for c in all_cols:
        # 10_000 is a high upper bound to avoid silent truncation; cap is local-side.
        items = zotero.get_collection_items(c.key, limit=10_000)
        vocabulary[c.key] = len(items)

    return json.dumps(
        {
            "item": {
                "item_key": item_key,
                "title": ref.title,
                "abstract_or_snippet": abstract,
                "current_collections": current_collections,
            },
            "collection_tree": [_collection_to_dict(c) for c in zotero.list_collections()],
            "vocabulary": vocabulary,
        }
    )


def _collection_to_dict(col: Collection) -> dict[str, Any]:
    return {
        "key": col.key,
        "name": col.name,
        "parent_key": col.parent_key,
        "children": [_collection_to_dict(c) for c in col.children],
    }
