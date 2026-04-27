"""Tests for the collection-editing tools and their private helpers.

Mirrors the structure of test_auto_tag.py: HTTP-layer tests live with
their existing client class in test_auto_tag.py; this file holds tool-
orchestration tests via a small _FakeApi / _FakeZotero pair.
"""

from __future__ import annotations

import json  # noqa: F401 — used by future tool test classes
from dataclasses import dataclass  # noqa: F401 — used by future tool test classes

import pytest  # noqa: F401 — used by future tool test classes

from mcp_local_reference.services.zotero_api_client import (
    CollectionSnapshot,
    ItemSnapshot,
    MissingCredentialsError,  # noqa: F401 — used by future tool test classes
    VersionConflictError,  # noqa: F401 — used by future tool test classes
    ZoteroApiError,  # noqa: F401 — used by future tool test classes
)
from mcp_local_reference.services.zotero_client import Collection, Reference
from mcp_local_reference.tools.collections import (
    MAX_ITEMS_PER_CALL,  # noqa: F401 — used by future tool test classes
    _check_cycle,
    _local_collection_snapshot,  # noqa: F401 — used by future tool test classes
    reparent_collection_impl,
)

# ======================================================================
# Fakes — collection-aware stand-ins for ZoteroApiClient and ZoteroClient
# ======================================================================


class _FakeApi:
    """Stand-in for ZoteroApiClient covering all 7 methods used by the tools."""

    def __init__(
        self,
        item_snapshots: dict[str, ItemSnapshot] | None = None,
        collection_snapshots: dict[str, CollectionSnapshot] | None = None,
        get_item_error: Exception | None = None,
        get_collection_error: Exception | None = None,
        update_item_error: Exception | None = None,
        update_collection_error: Exception | None = None,
        create_collection_error: Exception | None = None,
        delete_collection_error: Exception | None = None,
        new_version: int = 99,
        created_snapshot: CollectionSnapshot | None = None,
    ) -> None:
        self.item_snapshots = item_snapshots or {}
        self.collection_snapshots = collection_snapshots or {}
        self.get_item_error = get_item_error
        self.get_collection_error = get_collection_error
        self.update_item_error = update_item_error
        self.update_collection_error = update_collection_error
        self.create_collection_error = create_collection_error
        self.delete_collection_error = delete_collection_error
        self.new_version = new_version
        self.created_snapshot = created_snapshot
        self.update_item_calls: list[tuple[str, list[str], int]] = []
        self.update_collection_calls: list[tuple[str, dict, int]] = []
        self.create_collection_calls: list[tuple[str, str | None]] = []
        self.delete_collection_calls: list[tuple[str, int]] = []

    def get_item(self, item_key: str) -> ItemSnapshot:
        if self.get_item_error is not None:
            raise self.get_item_error
        return self.item_snapshots[item_key]

    def get_collection(self, key: str) -> CollectionSnapshot:
        if self.get_collection_error is not None:
            raise self.get_collection_error
        return self.collection_snapshots[key]

    def update_item_collections(
        self, item_key: str, collection_keys: list[str], version: int
    ) -> int:
        self.update_item_calls.append((item_key, list(collection_keys), version))
        if self.update_item_error is not None:
            raise self.update_item_error
        return self.new_version

    def update_collection(self, collection_key: str, *, version: int, **kwargs) -> int:
        self.update_collection_calls.append((collection_key, dict(kwargs), version))
        if self.update_collection_error is not None:
            raise self.update_collection_error
        return self.new_version

    def create_collection(self, name: str, parent_key: str | None) -> CollectionSnapshot:
        self.create_collection_calls.append((name, parent_key))
        if self.create_collection_error is not None:
            raise self.create_collection_error
        assert self.created_snapshot is not None
        return self.created_snapshot

    def delete_collection(self, collection_key: str, version: int) -> None:
        self.delete_collection_calls.append((collection_key, version))
        if self.delete_collection_error is not None:
            raise self.delete_collection_error


class _FakeZotero:
    """Stand-in for ZoteroClient — only the methods tools actually call."""

    def __init__(
        self,
        references: dict[str, Reference] | None = None,
        collections: list[Collection] | None = None,
        item_collections: dict[str, list[str]] | None = None,
        items_per_collection: dict[str, list[str]] | None = None,
    ) -> None:
        self.references = references or {}
        self.collections = collections or []
        self.item_collections = item_collections or {}
        self.items_per_collection = items_per_collection or {}

    def get_reference(self, item_key: str) -> Reference | None:
        return self.references.get(item_key)

    def list_collections(self) -> list[Collection]:
        return self.collections

    def get_item_collections(self, item_key: str) -> list[str]:
        return list(self.item_collections.get(item_key, []))

    def get_collection_items(self, collection_key: str, limit: int = 50):
        keys = self.items_per_collection.get(collection_key, [])
        return [self.references[k] for k in keys if k in self.references]


def _coll(key: str, name: str, parent: str | None = None) -> Collection:
    return Collection(key=key, name=name, parent_key=parent)


def _ref(item_key: str, title: str = "", abstract: str = "") -> Reference:
    return Reference(
        item_key=item_key,
        item_type="journalArticle",
        title=title,
        abstract=abstract,
        tags=[],
    )


# ======================================================================
# _check_cycle — local DAG walk used by reparent_collection
# ======================================================================


class TestCheckCycle:
    def test_no_cycle_for_unrelated_collection(self) -> None:
        # AI (no parent), Sinology (no parent). Reparent AI under Sinology — fine.
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI"), _coll("SINK2222", "Sinology")])
        assert _check_cycle(zotero, target_key="AIK11111", new_parent_key="SINK2222") is False

    def test_cycle_when_new_parent_is_target(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        assert _check_cycle(zotero, target_key="AIK11111", new_parent_key="AIK11111") is True

    def test_cycle_when_new_parent_is_descendant(self) -> None:
        # AI -> LLMs -> Transformers; reparent AI under Transformers => cycle.
        zotero = _FakeZotero(
            collections=[
                _coll("AIK11111", "AI"),
                _coll("LLMK2222", "LLMs", "AIK11111"),
                _coll("TRMK3333", "Transformers", "LLMK2222"),
            ]
        )
        assert _check_cycle(zotero, target_key="AIK11111", new_parent_key="TRMK3333") is True


# ======================================================================
# create_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import create_collection_impl  # noqa: E402


class TestCreateCollection:
    def test_dry_run_reports_preview(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi()
        result = json.loads(
            create_collection_impl(api, zotero, "LLMs", parent_key="AIK11111", dry_run=True)
        )
        assert result["status"] == "preview"
        assert result["would_create"]["name"] == "LLMs"
        assert result["would_create"]["parent_key"] == "AIK11111"
        assert api.create_collection_calls == []

    def test_already_exists_short_circuits_dry_run(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI"), _coll("LLMK2222", "LLMs", "AIK11111")]
        )
        result = json.loads(
            create_collection_impl(_FakeApi(), zotero, "LLMs", parent_key="AIK11111", dry_run=True)
        )
        assert result["status"] == "already_exists"
        assert result["existing_key"] == "LLMK2222"

    def test_parent_missing_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(
            create_collection_impl(_FakeApi(), zotero, "LLMs", parent_key="NOPE9999", dry_run=True)
        )
        assert "error" in result
        assert "NOPE9999" in result["error"]

    def test_write_creates_collection(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            created_snapshot=CollectionSnapshot(
                collection_key="LLMK2222",
                version=5,
                name="LLMs",
                parent_key="AIK11111",
                raw={},
            )
        )
        result = json.loads(
            create_collection_impl(api, zotero, "LLMs", parent_key="AIK11111", dry_run=False)
        )
        assert result["status"] == "applied"
        assert result["created"]["collection_key"] == "LLMK2222"
        assert result["new_version"] == 5
        assert api.create_collection_calls == [("LLMs", "AIK11111")]

    def test_write_short_circuits_when_already_exists(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI"), _coll("LLMK2222", "LLMs", "AIK11111")]
        )
        api = _FakeApi()
        result = json.loads(
            create_collection_impl(api, zotero, "LLMs", parent_key="AIK11111", dry_run=False)
        )
        assert result["status"] == "already_exists"
        assert api.create_collection_calls == []  # no POST issued

    def test_write_with_missing_credentials_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[])
        api = _FakeApi(create_collection_error=MissingCredentialsError("no creds"))
        result = json.loads(
            create_collection_impl(api, zotero, "Top", parent_key=None, dry_run=False)
        )
        assert "error" in result
        assert "no creds" in result["error"]


# ======================================================================
# rename_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import rename_collection_impl  # noqa: E402


class TestRenameCollection:
    def test_dry_run_reports_preview(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(
            rename_collection_impl(
                _FakeApi(), zotero, "AIK11111", "Artificial Intelligence", dry_run=True
            )
        )
        assert result["status"] == "preview"
        assert result["current"]["name"] == "AI"
        assert result["after"]["name"] == "Artificial Intelligence"

    def test_dry_run_returns_error_when_collection_missing(self) -> None:
        result = json.loads(
            rename_collection_impl(_FakeApi(), _FakeZotero(), "NOPE9999", "X", dry_run=True)
        )
        assert "error" in result

    def test_write_renames(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={"AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})},
            new_version=8,
        )
        result = json.loads(
            rename_collection_impl(
                api, zotero, "AIK11111", "Artificial Intelligence", dry_run=False
            )
        )
        assert result["status"] == "applied"
        assert result["new_version"] == 8
        assert api.update_collection_calls == [("AIK11111", {"name": "Artificial Intelligence"}, 7)]

    def test_write_no_op_when_name_unchanged(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={"AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})}
        )
        result = json.loads(rename_collection_impl(api, zotero, "AIK11111", "AI", dry_run=False))
        assert result["status"] == "no_changes"
        assert api.update_collection_calls == []

    def test_write_refuses_sibling_name_collision(self) -> None:
        zotero = _FakeZotero(
            collections=[
                _coll("AIK11111", "AI"),
                _coll("OTRK2222", "ML"),
            ]
        )
        result = json.loads(
            rename_collection_impl(_FakeApi(), zotero, "AIK11111", "ML", dry_run=False)
        )
        assert "error" in result
        assert "ML" in result["error"]

    def test_write_version_conflict_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={"AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})},
            update_collection_error=VersionConflictError("conflict"),
        )
        result = json.loads(rename_collection_impl(api, zotero, "AIK11111", "X", dry_run=False))
        assert "error" in result
        assert "conflict" in result["error"]


# ======================================================================
# reparent_collection_impl
# ======================================================================


class TestReparentCollection:
    def test_dry_run_reports_preview(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI"), _coll("OTRK2222", "Other")])
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "OTRK2222", dry_run=True)
        )
        assert result["status"] == "preview"
        assert result["after"]["parent_key"] == "OTRK2222"

    def test_dry_run_to_root(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI", "OTRK2222"), _coll("OTRK2222", "Other")]
        )
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", None, dry_run=True)
        )
        assert result["status"] == "preview"
        assert result["after"]["parent_key"] is None

    def test_dry_run_no_op_when_parent_unchanged(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI", "OTRK2222"), _coll("OTRK2222", "Other")]
        )
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "OTRK2222", dry_run=True)
        )
        assert result["status"] == "no_changes"

    def test_parent_missing_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "NOPE9999", dry_run=True)
        )
        assert "error" in result

    def test_cycle_to_self_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "AIK11111", dry_run=True)
        )
        assert "error" in result
        assert "Cycle" in result["error"]

    def test_cycle_to_descendant_returns_error(self) -> None:
        zotero = _FakeZotero(
            collections=[
                _coll("AIK11111", "AI"),
                _coll("LLMK2222", "LLMs", "AIK11111"),
            ]
        )
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "LLMK2222", dry_run=True)
        )
        assert "error" in result
        assert "Cycle" in result["error"]

    def test_write_reparents(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI"), _coll("OTRK2222", "Other")])
        api = _FakeApi(
            collection_snapshots={"AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})},
            new_version=8,
        )
        result = json.loads(
            reparent_collection_impl(api, zotero, "AIK11111", "OTRK2222", dry_run=False)
        )
        assert result["status"] == "applied"
        assert result["new_version"] == 8
        assert api.update_collection_calls == [("AIK11111", {"parent_key": "OTRK2222"}, 7)]


# ======================================================================
# delete_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import delete_collection_impl  # noqa: E402


class TestDeleteCollection:
    def test_dry_run_empty_collection(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(delete_collection_impl(_FakeApi(), zotero, "AIK11111", dry_run=True))
        assert result["status"] == "preview"
        assert result["would_orphan_items"] == []
        assert result["would_orphan_collections"] == []

    def test_dry_run_with_items_lists_them(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA"), "ITEMB": _ref("ITEMB")},
            items_per_collection={"AIK11111": ["ITEMA", "ITEMB"]},
        )
        result = json.loads(delete_collection_impl(_FakeApi(), zotero, "AIK11111", dry_run=True))
        assert sorted(result["would_orphan_items"]) == ["ITEMA", "ITEMB"]

    def test_dry_run_with_child_collections_lists_them(self) -> None:
        zotero = _FakeZotero(
            collections=[
                _coll("AIK11111", "AI"),
                _coll("LLMK2222", "LLMs", "AIK11111"),
                _coll("MMK33333", "Multimodal", "AIK11111"),
            ]
        )
        result = json.loads(delete_collection_impl(_FakeApi(), zotero, "AIK11111", dry_run=True))
        assert sorted(result["would_orphan_collections"]) == ["LLMK2222", "MMK33333"]

    def test_dry_run_returns_error_when_missing(self) -> None:
        result = json.loads(
            delete_collection_impl(_FakeApi(), _FakeZotero(), "NOPE9999", dry_run=True)
        )
        assert "error" in result

    def test_write_deletes(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={"AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})}
        )
        result = json.loads(delete_collection_impl(api, zotero, "AIK11111", dry_run=False))
        assert result["status"] == "applied"
        assert api.delete_collection_calls == [("AIK11111", 7)]

    def test_write_version_conflict_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={"AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})},
            delete_collection_error=VersionConflictError("conflict"),
        )
        result = json.loads(delete_collection_impl(api, zotero, "AIK11111", dry_run=False))
        assert "error" in result
        assert "conflict" in result["error"]


# ======================================================================
# add_items_to_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import (  # noqa: E402
    add_items_to_collection_impl,
)


class TestAddItemsToCollection:
    def test_rejects_empty_list(self) -> None:
        result = json.loads(
            add_items_to_collection_impl(_FakeApi(), _FakeZotero(), "AIK11111", [], dry_run=True)
        )
        assert "error" in result

    def test_rejects_too_many_items(self) -> None:
        too_many = [f"K{i:07d}" for i in range(MAX_ITEMS_PER_CALL + 1)]
        result = json.loads(
            add_items_to_collection_impl(
                _FakeApi(), _FakeZotero(), "AIK11111", too_many, dry_run=True
            )
        )
        assert "error" in result
        assert "exceeds" in result["error"]

    def test_dry_run_partitions_correctly(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA"), "ITEMB": _ref("ITEMB")},
            item_collections={"ITEMA": [], "ITEMB": ["AIK11111"]},
        )
        result = json.loads(
            add_items_to_collection_impl(
                _FakeApi(), zotero, "AIK11111", ["ITEMA", "ITEMB", "MISSING0"], dry_run=True
            )
        )
        assert result["status"] == "preview"
        assert result["would_add"] == ["ITEMA"]
        assert result["already_present"] == ["ITEMB"]
        assert result["not_found"] == ["MISSING0"]

    def test_collection_missing_returns_error(self) -> None:
        result = json.loads(
            add_items_to_collection_impl(
                _FakeApi(), _FakeZotero(), "NOPE9999", ["ITEMA"], dry_run=True
            )
        )
        assert "error" in result

    def test_write_succeeds_clean(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA")},
            item_collections={"ITEMA": []},
        )
        api = _FakeApi(
            item_snapshots={
                "ITEMA": ItemSnapshot(item_key="ITEMA", version=10, tags=[], collections=[], raw={})
            },
            new_version=11,
        )
        result = json.loads(
            add_items_to_collection_impl(api, zotero, "AIK11111", ["ITEMA"], dry_run=False)
        )
        assert result["status"] == "applied"
        assert result["succeeded"] == [{"item_key": "ITEMA", "new_version": 11}]
        assert result["failed"] == []
        assert api.update_item_calls == [("ITEMA", ["AIK11111"], 10)]

    def test_write_records_partial_failure(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA"), "ITEMB": _ref("ITEMB")},
            item_collections={"ITEMA": [], "ITEMB": []},
        )
        # First item ok; second raises VersionConflict.
        snapshots = {
            "ITEMA": ItemSnapshot("ITEMA", 10, [], [], {}),
            "ITEMB": ItemSnapshot("ITEMB", 20, [], [], {}),
        }

        class _PartialApi(_FakeApi):
            def update_item_collections(self, item_key, collection_keys, version):
                self.update_item_calls.append((item_key, list(collection_keys), version))
                if item_key == "ITEMB":
                    raise VersionConflictError("conflict on ITEMB")
                return self.new_version

        api = _PartialApi(item_snapshots=snapshots, new_version=11)
        result = json.loads(
            add_items_to_collection_impl(api, zotero, "AIK11111", ["ITEMA", "ITEMB"], dry_run=False)
        )
        assert result["status"] == "partial"
        assert result["succeeded"] == [{"item_key": "ITEMA", "new_version": 11}]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["item_key"] == "ITEMB"

    def test_write_short_circuits_when_all_already_present(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA")},
            item_collections={"ITEMA": ["AIK11111"]},
        )
        api = _FakeApi()
        result = json.loads(
            add_items_to_collection_impl(api, zotero, "AIK11111", ["ITEMA"], dry_run=False)
        )
        assert result["status"] == "no_changes"
        assert api.update_item_calls == []


# ======================================================================
# remove_items_from_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import (  # noqa: E402
    remove_items_from_collection_impl,
)


class TestRemoveItemsFromCollection:
    def test_rejects_empty_list(self) -> None:
        result = json.loads(
            remove_items_from_collection_impl(
                _FakeApi(), _FakeZotero(), "AIK11111", [], dry_run=True
            )
        )
        assert "error" in result

    def test_rejects_too_many_items(self) -> None:
        too_many = [f"K{i:07d}" for i in range(MAX_ITEMS_PER_CALL + 1)]
        result = json.loads(
            remove_items_from_collection_impl(
                _FakeApi(), _FakeZotero(), "AIK11111", too_many, dry_run=True
            )
        )
        assert "error" in result

    def test_dry_run_partitions(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA"), "ITEMB": _ref("ITEMB")},
            item_collections={"ITEMA": ["AIK11111"], "ITEMB": []},
        )
        result = json.loads(
            remove_items_from_collection_impl(
                _FakeApi(), zotero, "AIK11111", ["ITEMA", "ITEMB"], dry_run=True
            )
        )
        assert result["status"] == "preview"
        assert result["would_remove"] == ["ITEMA"]
        assert result["not_present"] == ["ITEMB"]

    def test_write_succeeds_clean(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA")},
            item_collections={"ITEMA": ["AIK11111", "OTHKEY99"]},
        )
        api = _FakeApi(
            item_snapshots={"ITEMA": ItemSnapshot("ITEMA", 10, [], ["AIK11111", "OTHKEY99"], {})},
            new_version=11,
        )
        result = json.loads(
            remove_items_from_collection_impl(api, zotero, "AIK11111", ["ITEMA"], dry_run=False)
        )
        assert result["status"] == "applied"
        assert api.update_item_calls == [("ITEMA", ["OTHKEY99"], 10)]

    def test_write_short_circuits_when_all_not_present(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA")},
            item_collections={"ITEMA": []},
        )
        api = _FakeApi()
        result = json.loads(
            remove_items_from_collection_impl(api, zotero, "AIK11111", ["ITEMA"], dry_run=False)
        )
        assert result["status"] == "no_changes"
        assert api.update_item_calls == []


# ======================================================================
# suggest_collection_placement_impl
# ======================================================================


from mcp_local_reference.tools.collections import (  # noqa: E402
    suggest_collection_placement_impl,
)


class _NoOpPdf:
    """Stand-in for PdfProcessor — placement suggester only needs first-page text."""

    def first_page_text(self, path) -> str:  # pragma: no cover (stubbed)
        return ""


class TestSuggestCollectionPlacement:
    def test_returns_title_abstract_tree_and_counts(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI"), _coll("LLMK2222", "LLMs", "AIK11111")],
            references={"ITEMA": _ref("ITEMA", title="Title A", abstract="Abstract about LLMs.")},
            item_collections={"ITEMA": ["AIK11111"]},
            items_per_collection={"AIK11111": ["ITEMA"], "LLMK2222": []},
        )
        result = json.loads(suggest_collection_placement_impl(zotero, _NoOpPdf(), "ITEMA"))
        assert result["item"]["title"] == "Title A"
        assert "LLMs" in result["item"]["abstract_or_snippet"]
        assert result["item"]["current_collections"] == [{"key": "AIK11111", "name": "AI"}]
        assert any(c["key"] == "AIK11111" for c in result["collection_tree"])
        assert result["vocabulary"]["AIK11111"] == 1
        assert result["vocabulary"]["LLMK2222"] == 0

    def test_falls_back_to_pdf_snippet_when_abstract_empty(self) -> None:
        class _Pdf:
            def first_page_text(self, path) -> str:
                return "First page snippet text."

        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA", title="T", abstract="")},
            item_collections={"ITEMA": []},
            items_per_collection={"AIK11111": []},
        )
        # The impl needs a PDF path lookup — stub get_pdf_path on the fake.
        zotero.get_pdf_path = lambda k: "/tmp/fake.pdf"  # type: ignore[attr-defined]

        result = json.loads(suggest_collection_placement_impl(zotero, _Pdf(), "ITEMA"))
        assert "First page snippet text." in result["item"]["abstract_or_snippet"]

    def test_returns_error_when_item_missing(self) -> None:
        result = json.loads(
            suggest_collection_placement_impl(_FakeZotero(), _NoOpPdf(), "NOPE9999")
        )
        assert "error" in result
