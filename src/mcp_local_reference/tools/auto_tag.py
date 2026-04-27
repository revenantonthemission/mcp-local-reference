"""Auto-tagging tools — read context for Claude, then write tags via the Zotero Web API.

Designed for the human-in-the-loop pattern: ``suggest_tags_context`` gathers
everything Claude needs to propose tags; Claude reasons in between; ``apply_tags``
writes additions; ``remove_tags`` writes removals. Both write tools default to
dry-run so a typo doesn't silently mutate the user's library.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.services.pdf_processor import PdfProcessor
from mcp_local_reference.services.zotero_api_client import (
    ItemSnapshot,
    MissingCredentialsError,
    VersionConflictError,
    ZoteroApiClient,
    ZoteroApiError,
)
from mcp_local_reference.services.zotero_client import ZoteroClient

MAX_TAGS_PER_CALL = 25
PDF_SNIPPET_CHAR_CAP = 2000
DEFAULT_VOCAB_LIMIT = 30


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register auto-tagging tools on *mcp*."""
    zotero = ZoteroClient(config)
    api = ZoteroApiClient(config)
    pdf = PdfProcessor(min_figure_pixels=config.min_figure_pixels)

    _register_suggest_tags_context(mcp, zotero, pdf)
    _register_apply_tags(mcp, api, zotero)
    _register_remove_tags(mcp, api, zotero)


def _register_suggest_tags_context(mcp: FastMCP, zotero: ZoteroClient, pdf: PdfProcessor) -> None:
    @mcp.tool()
    def suggest_tags_context(item_key: str) -> str:
        """Gather everything Claude needs to suggest tags for one Zotero item.

        Returns the item's title, abstract (or first-page PDF snippet as a
        fallback when the abstract is empty), its current tags, and the
        most-used tags in your library — so suggestions can be anchored to
        your existing vocabulary instead of inventing synonyms.

        Args:
            item_key: The 8-character Zotero item key.
        """
        return suggest_tags_context_impl(zotero, pdf, item_key)


def _register_apply_tags(mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient) -> None:
    @mcp.tool()
    def apply_tags(item_key: str, tags: list[str], dry_run: bool = True) -> str:
        """Add tags to a Zotero item via the Web API (append-only merge).

        Defaults to ``dry_run=True`` — pass ``dry_run=False`` to actually
        write. Tags are merged with the item's existing tags via set union;
        nothing is removed by this tool. Uses optimistic concurrency
        (``If-Unmodified-Since-Version``) so concurrent edits fail loudly
        rather than silently overwriting.

        Dry-run reads ``current_tags`` from the local Zotero SQLite (no
        Web API credentials required), which makes prompt iteration
        cheap. The real write always re-fetches from the Web API for
        the authoritative version number.

        Args:
            item_key: The 8-character Zotero item key.
            tags: Tags to add. Empty strings and duplicates are ignored.
            dry_run: If True (default), report the diff without writing.
        """
        return apply_tags_impl(api, zotero, item_key, tags, dry_run)


def _register_remove_tags(mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient) -> None:
    @mcp.tool()
    def remove_tags(item_key: str, tags: list[str], dry_run: bool = True) -> str:
        """Remove tags from a Zotero item via the Web API.

        Mirrors apply_tags but in reverse: tags listed here are *removed*
        from the item's tag list. Defaults to dry_run=True. Idempotent —
        tags not present on the item are silently skipped and reported
        under not_present in the response. Per-call cap matches apply_tags
        (25) to guard against runaway batch destruction.

        Designed for fixing tags that auto-tagging or import workflows
        added incorrectly. Should NOT be used on Claude's own judgment —
        only invoke when the user explicitly identifies tags to remove.

        Args:
            item_key: The 8-character Zotero item key.
            tags: Tags to remove. Empty strings and duplicates are ignored.
                Tags not currently on the item are silently skipped.
            dry_run: If True (default), report the diff without writing.
        """
        return remove_tags_impl(api, zotero, item_key, tags, dry_run)


# ======================================================================
# Implementations — module-level so they're directly unit-testable.
# ======================================================================


def suggest_tags_context_impl(zotero: ZoteroClient, pdf: PdfProcessor, item_key: str) -> str:
    ref = zotero.get_reference(item_key)
    if ref is None:
        return json.dumps({"error": f"Reference '{item_key}' not found"})

    snippet = _pdf_first_page_snippet(zotero, pdf, item_key) if not ref.abstract else ""
    vocabulary = [
        {"name": name, "uses": uses} for name, uses in zotero.top_tags(limit=DEFAULT_VOCAB_LIMIT)
    ]
    payload = {
        "item_key": item_key,
        "title": ref.title,
        "abstract": ref.abstract,
        "pdf_snippet": snippet,
        "current_tags": ref.tags,
        "vocabulary": vocabulary,
        "guidance": (
            f"Suggest 3–7 tags. Prefer reusing names from `vocabulary`. "
            f"`apply_tags` rejects more than {MAX_TAGS_PER_CALL} tags per call "
            f"and merges with `current_tags` (append-only — nothing is removed). "
            f"Use `remove_tags` to undo tags that were added in error."
        ),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _pdf_first_page_snippet(zotero: ZoteroClient, pdf: PdfProcessor, item_key: str) -> str:
    pdf_path = zotero.get_pdf_path(item_key)
    if pdf_path is None:
        return ""
    try:
        text = pdf.extract_text(pdf_path, start_page=0, end_page=1)
    except Exception:
        return ""
    return text[:PDF_SNIPPET_CHAR_CAP]


def _validate_tags_input(tags: list[str], action: str) -> tuple[list[str], str | None]:
    """Normalise *tags* and enforce the per-call cap.

    Returns ``(proposed, None)`` on success or ``([], error_json)`` on
    failure, where *action* is a short noun used in the error message
    (e.g. ``"tagging"`` or ``"removal"``).
    """
    proposed = sorted({t.strip() for t in tags if t and t.strip()})
    if not proposed:
        return [], json.dumps({"error": "No non-empty tags provided"})
    if len(proposed) > MAX_TAGS_PER_CALL:
        return [], json.dumps(
            {
                "error": (
                    f"Refusing: {len(proposed)} tags exceeds the per-call cap of "
                    f"{MAX_TAGS_PER_CALL} (guards against runaway batch {action})."
                )
            }
        )
    return proposed, None


class _SnapshotError(RuntimeError):
    """Internal sentinel: snapshot resolution failed; carries the JSON error payload."""

    def __init__(self, error_json: str) -> None:
        super().__init__(error_json)
        self.error_json = error_json


def _resolve_snapshot(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    item_key: str,
    dry_run: bool,
) -> ItemSnapshot:
    """Return the snapshot, or raise ``_SnapshotError`` carrying the JSON error.

    Dry-run reads from local SQLite (no credentials needed). The write path
    fetches from the Web API to get the authoritative version number.
    """
    if dry_run:
        snapshot = _local_snapshot(zotero, item_key)
        if snapshot is None:
            raise _SnapshotError(json.dumps({"error": f"Reference '{item_key}' not found"}))
        return snapshot
    try:
        return api.get_item(item_key)
    except MissingCredentialsError as e:
        raise _SnapshotError(json.dumps({"error": str(e)})) from e
    except ZoteroApiError as e:
        raise _SnapshotError(json.dumps({"error": str(e)})) from e


def apply_tags_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    item_key: str,
    tags: list[str],
    dry_run: bool,
) -> str:
    proposed, err = _validate_tags_input(tags, "tagging")
    if err is not None:
        return err

    try:
        snapshot = _resolve_snapshot(api, zotero, item_key, dry_run)
    except _SnapshotError as e:
        return e.error_json

    return _build_and_maybe_write(api, snapshot, item_key, proposed, dry_run)


def _local_snapshot(zotero: ZoteroClient, item_key: str) -> ItemSnapshot | None:
    """Build an ItemSnapshot from local SQLite for dry-run previews.

    Returns version=0 deliberately: a local snapshot must never be used
    for a write, and version=0 guarantees that any accidental ``set_tags``
    call would fail with HTTP 412 rather than silently corrupting data.
    """
    ref = zotero.get_reference(item_key)
    if ref is None:
        return None
    return ItemSnapshot(item_key=item_key, version=0, tags=ref.tags, collections=[], raw={})


def _build_and_maybe_write(
    api: ZoteroApiClient,
    snapshot: ItemSnapshot,
    item_key: str,
    proposed: list[str],
    dry_run: bool,
) -> str:
    current = set(snapshot.tags)
    proposed_set = set(proposed)
    new_tags = sorted(proposed_set - current)
    already_present = sorted(proposed_set & current)
    union = sorted(current | proposed_set)

    plan: dict[str, Any] = {
        "item_key": item_key,
        "current_tags": sorted(current),
        "would_add": new_tags,
        "already_present": already_present,
        "after_apply": union,
        "dry_run": dry_run,
    }

    if dry_run:
        plan["status"] = "preview"
        return json.dumps(plan, indent=2, ensure_ascii=False)
    if not new_tags:
        plan["status"] = "no_changes"
        return json.dumps(plan, indent=2, ensure_ascii=False)

    try:
        new_version = api.set_tags(item_key, union, snapshot.version)
    except VersionConflictError as e:
        return json.dumps({"error": str(e), "hint": "Re-run apply_tags to retry."})
    except ZoteroApiError as e:
        return json.dumps({"error": str(e)})

    plan["status"] = "applied"
    plan["new_version"] = new_version
    plan["added_count"] = len(new_tags)
    return json.dumps(plan, indent=2, ensure_ascii=False)


def remove_tags_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    item_key: str,
    tags: list[str],
    dry_run: bool,
) -> str:
    proposed, err = _validate_tags_input(tags, "removal")
    if err is not None:
        return err

    try:
        snapshot = _resolve_snapshot(api, zotero, item_key, dry_run)
    except _SnapshotError as e:
        return e.error_json

    return _build_remove_plan(snapshot, item_key, proposed, dry_run, api)


def _build_remove_plan(
    snapshot: ItemSnapshot,
    item_key: str,
    proposed: list[str],
    dry_run: bool,
    api: ZoteroApiClient | None,
) -> str:
    current = set(snapshot.tags)
    proposed_set = set(proposed)
    would_remove = sorted(proposed_set & current)
    not_present = sorted(proposed_set - current)
    after_apply = sorted(current - proposed_set)

    plan: dict[str, Any] = {
        "item_key": item_key,
        "current_tags": sorted(current),
        "would_remove": would_remove,
        "not_present": not_present,
        "after_apply": after_apply,
        "dry_run": dry_run,
    }

    if dry_run:
        plan["status"] = "preview"
        return json.dumps(plan, indent=2, ensure_ascii=False)
    if not would_remove:
        plan["status"] = "no_changes"
        return json.dumps(plan, indent=2, ensure_ascii=False)

    assert api is not None  # write path always passes a real api
    try:
        new_version = api.set_tags(item_key, after_apply, snapshot.version)
    except VersionConflictError as e:
        return json.dumps({"error": str(e), "hint": "Re-run remove_tags to retry."})
    except ZoteroApiError as e:
        return json.dumps({"error": str(e)})

    plan["status"] = "applied"
    plan["new_version"] = new_version
    plan["removed_count"] = len(would_remove)
    return json.dumps(plan, indent=2, ensure_ascii=False)
