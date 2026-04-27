"""Tests for auto-tagging tools and the Zotero Web API client.

The HTTP layer is exercised via ``httpx.MockTransport`` (no real HTTP, no
new test deps). Orchestration logic in ``apply_tags_impl`` uses a small
``_FakeApi`` stand-in so tests don't go through HTTP at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mcp_local_reference.config import Config
from mcp_local_reference.services.pdf_processor import PdfProcessor
from mcp_local_reference.services.zotero_api_client import (
    ItemSnapshot,
    MissingCredentialsError,
    VersionConflictError,
    ZoteroApiClient,
    ZoteroApiError,
)
from mcp_local_reference.services.zotero_client import Reference, ZoteroClient
from mcp_local_reference.tools.auto_tag import (
    MAX_TAGS_PER_CALL,
    apply_tags_impl,
    remove_tags_impl,
    suggest_tags_context_impl,
)

# ======================================================================
# top_tags helper on ZoteroClient
# ======================================================================


class TestTopTags:
    def test_returns_tags_with_counts(self, config: Config) -> None:
        tags = ZoteroClient(config).top_tags()
        names = {name for name, _ in tags}
        assert names == {"deep-learning", "nlp"}
        assert all(uses == 1 for _, uses in tags)

    def test_respects_limit(self, config: Config) -> None:
        tags = ZoteroClient(config).top_tags(limit=1)
        assert len(tags) == 1


# ======================================================================
# ZoteroApiClient — HTTP layer with MockTransport
# ======================================================================


@pytest.fixture
def api_config(mock_zotero_db: Path, tmp_dir: Path) -> Config:
    return Config(
        zotero_data_dir=mock_zotero_db.parent,
        data_dir=tmp_dir / "mcp",
        zotero_user_id="42",
        zotero_api_key="test-key",
        zotero_api_base_url="https://api.test",
    )


class TestZoteroApiClient:
    def test_get_item_returns_snapshot(self, api_config: Config) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["api_key"] = request.headers.get("Zotero-API-Key", "")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "version": 17,
                        "tags": [{"tag": "ml"}, {"tag": "papers"}],
                        "collections": ["COLLAAA1", "COLLBBB2"],
                    }
                },
            )

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        snap = client.get_item("ABCD1234")

        assert snap.version == 17
        assert snap.tags == ["ml", "papers"]
        assert snap.collections == ["COLLAAA1", "COLLBBB2"]
        assert "users/42/items/ABCD1234" in captured["url"]
        assert captured["api_key"] == "test-key"

    def test_get_item_404_raises(self, api_config: Config) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(404, json={}))
        client = ZoteroApiClient(api_config, transport=transport)
        with pytest.raises(ZoteroApiError):
            client.get_item("BADKEY")

    def test_set_tags_sends_patch_with_version_header(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["unmod"] = request.headers.get("If-Unmodified-Since-Version")
            captured["body"] = json.loads(request.content)
            return httpx.Response(204, headers={"Last-Modified-Version": "18"})

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        new_version = client.set_tags("ABCD1234", ["ml", "rl"], version=17)

        assert new_version == 18
        assert captured["method"] == "PATCH"
        assert captured["unmod"] == "17"
        assert captured["body"] == {"tags": [{"tag": "ml"}, {"tag": "rl"}]}

    def test_set_tags_412_raises_conflict(self, api_config: Config) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(412))
        client = ZoteroApiClient(api_config, transport=transport)
        with pytest.raises(VersionConflictError):
            client.set_tags("ABCD1234", ["x"], version=17)

    def test_missing_credentials_raises(self, mock_zotero_db: Path, tmp_dir: Path) -> None:
        config = Config(
            zotero_data_dir=mock_zotero_db.parent,
            data_dir=tmp_dir / "mcp",
            zotero_user_id="",
            zotero_api_key="",
        )
        with pytest.raises(MissingCredentialsError):
            ZoteroApiClient(config).get_item("ABCD1234")

    def test_collection_snapshot_dataclass_shape(self) -> None:
        from mcp_local_reference.services.zotero_api_client import CollectionSnapshot

        snap = CollectionSnapshot(
            collection_key="COLL1234",
            version=42,
            name="AI",
            parent_key=None,
            raw={"key": "COLL1234"},
        )
        assert snap.collection_key == "COLL1234"
        assert snap.version == 42
        assert snap.name == "AI"
        assert snap.parent_key is None
        assert snap.raw == {"key": "COLL1234"}

    def test_create_collection_posts_and_returns_snapshot(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "successful": {
                        "0": {
                            "key": "NEWKEYAA",
                            "version": 5,
                            "data": {
                                "key": "NEWKEYAA",
                                "version": 5,
                                "name": "AI",
                                "parentCollection": "PARENTKK",
                            },
                        }
                    },
                    "success": {"0": "NEWKEYAA"},
                    "failed": {},
                    "unchanged": {},
                },
            )

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        snap = client.create_collection("AI", parent_key="PARENTKK")

        assert snap.collection_key == "NEWKEYAA"
        assert snap.version == 5
        assert snap.name == "AI"
        assert snap.parent_key == "PARENTKK"
        assert captured["method"] == "POST"
        assert "users/42/collections" in str(captured["url"])
        assert captured["body"] == [{"name": "AI", "parentCollection": "PARENTKK"}]

    def test_create_collection_at_root_sends_false_parent(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "successful": {
                        "0": {
                            "key": "ROOTKEYY",
                            "version": 1,
                            "data": {
                                "key": "ROOTKEYY",
                                "version": 1,
                                "name": "TopLevel",
                                "parentCollection": False,
                            },
                        }
                    },
                    "success": {"0": "ROOTKEYY"},
                    "failed": {},
                    "unchanged": {},
                },
            )

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        snap = client.create_collection("TopLevel", parent_key=None)

        assert snap.parent_key is None
        assert captured["body"] == [{"name": "TopLevel", "parentCollection": False}]

    def test_get_collection_returns_snapshot(self, api_config: Config) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "key": "COLL1234",
                        "version": 9,
                        "name": "Sinology",
                        "parentCollection": False,
                    }
                },
            )

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        snap = client.get_collection("COLL1234")
        assert snap.collection_key == "COLL1234"
        assert snap.version == 9
        assert snap.name == "Sinology"
        assert snap.parent_key is None

    def test_update_collection_rename_only(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["unmod"] = request.headers.get("If-Unmodified-Since-Version")
            captured["body"] = json.loads(request.content)
            return httpx.Response(204, headers={"Last-Modified-Version": "11"})

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        new_version = client.update_collection(
            "COLL1234", name="Artificial Intelligence", version=10
        )

        assert new_version == 11
        assert captured["method"] == "PATCH"
        assert captured["unmod"] == "10"
        assert captured["body"] == {"name": "Artificial Intelligence"}

    def test_update_collection_reparent_to_root(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(204, headers={"Last-Modified-Version": "12"})

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        client.update_collection("COLL1234", parent_key=None, version=11)

        assert captured["body"] == {"parentCollection": False}

    def test_update_collection_reparent_to_other(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(204, headers={"Last-Modified-Version": "13"})

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        client.update_collection("COLL1234", parent_key="NEWPARNT", version=12)

        assert captured["body"] == {"parentCollection": "NEWPARNT"}

    def test_update_collection_412_raises_conflict(self, api_config: Config) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(412))
        client = ZoteroApiClient(api_config, transport=transport)
        with pytest.raises(VersionConflictError):
            client.update_collection("COLL1234", name="X", version=10)

    def test_delete_collection_sends_delete_with_version(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["unmod"] = request.headers.get("If-Unmodified-Since-Version")
            return httpx.Response(204)

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        client.delete_collection("COLL1234", version=15)

        assert captured["method"] == "DELETE"
        assert "users/42/collections/COLL1234" in str(captured["url"])
        assert captured["unmod"] == "15"

    def test_delete_collection_412_raises_conflict(self, api_config: Config) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(412))
        client = ZoteroApiClient(api_config, transport=transport)
        with pytest.raises(VersionConflictError):
            client.delete_collection("COLL1234", version=15)

    def test_update_item_collections_sends_patch_with_version(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["unmod"] = request.headers.get("If-Unmodified-Since-Version")
            captured["body"] = json.loads(request.content)
            return httpx.Response(204, headers={"Last-Modified-Version": "21"})

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        new_version = client.update_item_collections(
            "ITEMAAAA", ["COLL1111", "COLL2222"], version=20
        )

        assert new_version == 21
        assert captured["method"] == "PATCH"
        assert "users/42/items/ITEMAAAA" in str(captured["url"])
        assert captured["unmod"] == "20"
        assert captured["body"] == {"collections": ["COLL1111", "COLL2222"]}


# ======================================================================
# apply_tags_impl — orchestration via a fake API
# ======================================================================


class _FakeApi:
    """Minimal stand-in for ZoteroApiClient used by apply_tags_impl tests."""

    def __init__(
        self,
        snapshot: ItemSnapshot | None = None,
        get_error: Exception | None = None,
        set_error: Exception | None = None,
        new_version: int = 99,
    ) -> None:
        self.snapshot = snapshot
        self.get_error = get_error
        self.set_error = set_error
        self.new_version = new_version
        self.set_tags_calls: list[tuple[str, list[str], int]] = []

    def get_item(self, item_key: str) -> ItemSnapshot:
        if self.get_error is not None:
            raise self.get_error
        assert self.snapshot is not None
        return self.snapshot

    def set_tags(self, item_key: str, tags: list[str], version: int) -> int:
        self.set_tags_calls.append((item_key, list(tags), version))
        if self.set_error is not None:
            raise self.set_error
        return self.new_version


class _FakeZotero:
    """Minimal stand-in for ZoteroClient — only get_reference is exercised."""

    def __init__(self, references: dict[str, Reference] | None = None) -> None:
        self.references = references or {}

    def get_reference(self, item_key: str) -> Reference | None:
        return self.references.get(item_key)


def _ref(item_key: str, tags: list[str]) -> Reference:
    return Reference(item_key=item_key, item_type="journalArticle", tags=tags)


class TestApplyTags:
    def test_rejects_empty_tags(self) -> None:
        result = json.loads(
            apply_tags_impl(_FakeApi(), _FakeZotero(), "K", ["", "  "], dry_run=True)  # type: ignore[arg-type]
        )
        assert "error" in result

    def test_rejects_too_many_tags(self) -> None:
        too_many = [f"t{i}" for i in range(MAX_TAGS_PER_CALL + 1)]
        result = json.loads(
            apply_tags_impl(_FakeApi(), _FakeZotero(), "K", too_many, dry_run=True)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "exceeds" in result["error"]

    def test_dry_run_reads_local_and_skips_api(self) -> None:
        # _FakeApi() with no snapshot would AssertionError if get_item were called —
        # so a passing test proves the dry-run path never hits the API.
        api = _FakeApi()
        zotero = _FakeZotero({"K": _ref("K", ["existing"])})
        result = json.loads(
            apply_tags_impl(api, zotero, "K", ["new", "existing"], dry_run=True)  # type: ignore[arg-type]
        )
        assert result["status"] == "preview"
        assert result["would_add"] == ["new"]
        assert result["already_present"] == ["existing"]
        assert result["after_apply"] == ["existing", "new"]
        assert api.set_tags_calls == []

    def test_dry_run_works_without_credentials(self) -> None:
        # If the credential check were still on the dry-run path, this would fail.
        api = _FakeApi(get_error=MissingCredentialsError("set creds"))
        zotero = _FakeZotero({"K": _ref("K", [])})
        result = json.loads(
            apply_tags_impl(api, zotero, "K", ["new"], dry_run=True)  # type: ignore[arg-type]
        )
        assert result["status"] == "preview"
        assert result["would_add"] == ["new"]
        assert "error" not in result

    def test_dry_run_returns_error_when_item_missing_locally(self) -> None:
        api = _FakeApi()
        zotero = _FakeZotero()  # empty — no items
        result = json.loads(
            apply_tags_impl(api, zotero, "MISSING", ["new"], dry_run=True)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "MISSING" in result["error"]

    def test_apply_writes_union(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["a"], collections=[], raw={}),
            new_version=6,
        )
        zotero = _FakeZotero()  # not used when dry_run=False
        result = json.loads(
            apply_tags_impl(api, zotero, "K", ["b", "c"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "applied"
        assert result["new_version"] == 6
        assert result["added_count"] == 2
        assert api.set_tags_calls == [("K", ["a", "b", "c"], 5)]

    def test_apply_skips_write_when_no_new_tags(self) -> None:
        snap = ItemSnapshot(item_key="K", version=5, tags=["a", "b"], collections=[], raw={})
        api = _FakeApi(snapshot=snap)
        zotero = _FakeZotero()
        result = json.loads(
            apply_tags_impl(api, zotero, "K", ["a", "b"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "no_changes"
        assert api.set_tags_calls == []

    def test_apply_with_missing_credentials_returns_error(self) -> None:
        api = _FakeApi(get_error=MissingCredentialsError("set creds"))
        zotero = _FakeZotero()
        result = json.loads(
            apply_tags_impl(api, zotero, "K", ["x"], dry_run=False)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "creds" in result["error"]
        assert api.set_tags_calls == []  # never reach the write path

    def test_version_conflict_returns_hint(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=[], collections=[], raw={}),
            set_error=VersionConflictError("conflict"),
        )
        zotero = _FakeZotero()
        result = json.loads(
            apply_tags_impl(api, zotero, "K", ["x"], dry_run=False)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "hint" in result


# ======================================================================
# remove_tags_impl — orchestration via the same fakes
# ======================================================================


class TestRemoveTags:
    def test_rejects_empty_tags(self) -> None:
        result = json.loads(
            remove_tags_impl(_FakeApi(), _FakeZotero(), "K", ["", "  "], dry_run=True)  # type: ignore[arg-type]
        )
        assert "error" in result

    def test_rejects_too_many_tags(self) -> None:
        too_many = [f"t{i}" for i in range(MAX_TAGS_PER_CALL + 1)]
        result = json.loads(
            remove_tags_impl(_FakeApi(), _FakeZotero(), "K", too_many, dry_run=True)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "exceeds" in result["error"]

    def test_dry_run_reads_local_and_skips_api(self) -> None:
        # _FakeApi() with no snapshot would AssertionError if get_item were called —
        # passing this test proves the dry-run path never hits the API.
        api = _FakeApi()
        zotero = _FakeZotero({"K": _ref("K", ["existing", "to-remove"])})
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["to-remove", "absent"], dry_run=True)  # type: ignore[arg-type]
        )
        assert result["status"] == "preview"
        assert result["would_remove"] == ["to-remove"]
        assert result["not_present"] == ["absent"]
        assert result["after_apply"] == ["existing"]
        assert api.set_tags_calls == []

    def test_dry_run_works_without_credentials(self) -> None:
        # If the credential check were on the dry-run path, this would fail.
        api = _FakeApi(get_error=MissingCredentialsError("set creds"))
        zotero = _FakeZotero({"K": _ref("K", ["x"])})
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["x"], dry_run=True)  # type: ignore[arg-type]
        )
        assert result["status"] == "preview"
        assert result["would_remove"] == ["x"]
        assert "error" not in result

    def test_dry_run_returns_error_when_item_missing_locally(self) -> None:
        api = _FakeApi()
        zotero = _FakeZotero()  # empty — no items
        result = json.loads(
            remove_tags_impl(api, zotero, "MISSING", ["x"], dry_run=True)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "MISSING" in result["error"]

    def test_remove_writes_diff(self) -> None:
        snap = ItemSnapshot(item_key="K", version=5, tags=["a", "b", "c"], collections=[], raw={})
        api = _FakeApi(
            snapshot=snap,
            new_version=6,
        )
        zotero = _FakeZotero()  # not used when dry_run=False
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["b"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "applied"
        assert result["new_version"] == 6
        assert result["removed_count"] == 1
        # set_tags called with current minus removed, sorted, and the original version
        assert api.set_tags_calls == [("K", ["a", "c"], 5)]

    def test_remove_skips_write_when_no_tags_match(self) -> None:
        snap = ItemSnapshot(item_key="K", version=5, tags=["a", "b"], collections=[], raw={})
        api = _FakeApi(snapshot=snap)
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["x", "y"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "no_changes"
        assert result["would_remove"] == []
        assert sorted(result["not_present"]) == ["x", "y"]
        assert api.set_tags_calls == []  # no PATCH issued

    def test_remove_idempotent_partial_match(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["a", "b"], collections=[], raw={}),
            new_version=6,
        )
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["b", "absent"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "applied"
        assert result["would_remove"] == ["b"]
        assert result["not_present"] == ["absent"]
        assert result["after_apply"] == ["a"]
        assert api.set_tags_calls == [("K", ["a"], 5)]

    def test_remove_can_clear_all_tags(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["a", "b"], collections=[], raw={}),
            new_version=6,
        )
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["a", "b"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "applied"
        assert result["after_apply"] == []
        assert api.set_tags_calls == [("K", [], 5)]

    def test_remove_with_missing_credentials_returns_error(self) -> None:
        api = _FakeApi(get_error=MissingCredentialsError("set creds"))
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["x"], dry_run=False)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "creds" in result["error"]
        assert api.set_tags_calls == []  # never reach the write path

    def test_remove_version_conflict_returns_hint(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["x"], collections=[], raw={}),
            set_error=VersionConflictError("conflict"),
        )
        zotero = _FakeZotero()
        result = json.loads(
            remove_tags_impl(api, zotero, "K", ["x"], dry_run=False)  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "hint" in result


# ======================================================================
# suggest_tags_context_impl — uses real ZoteroClient against the mock DB
# ======================================================================


class TestSuggestTagsContext:
    def test_returns_error_for_missing_item(self, config: Config) -> None:
        result = json.loads(suggest_tags_context_impl(ZoteroClient(config), PdfProcessor(), "NOPE"))
        assert "error" in result

    def test_returns_metadata_for_existing_item(self, config: Config) -> None:
        result = json.loads(
            suggest_tags_context_impl(ZoteroClient(config), PdfProcessor(), "TESTKEY1")
        )
        assert result["item_key"] == "TESTKEY1"
        assert "Deep Learning" in result["title"]
        assert result["abstract"]
        # Abstract is present, so the PDF fallback is intentionally skipped.
        assert result["pdf_snippet"] == ""
        assert set(result["current_tags"]) == {"deep-learning", "nlp"}
        vocab_names = {v["name"] for v in result["vocabulary"]}
        assert vocab_names == {"deep-learning", "nlp"}
