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
from mcp_local_reference.services.zotero_client import ZoteroClient
from mcp_local_reference.tools.auto_tag import (
    MAX_TAGS_PER_CALL,
    apply_tags_impl,
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
                    }
                },
            )

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        snap = client.get_item("ABCD1234")

        assert snap.version == 17
        assert snap.tags == ["ml", "papers"]
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


class TestApplyTags:
    def test_rejects_empty_tags(self) -> None:
        result = json.loads(apply_tags_impl(_FakeApi(), "K", ["", "  "], dry_run=False))  # type: ignore[arg-type]
        assert "error" in result

    def test_rejects_too_many_tags(self) -> None:
        too_many = [f"t{i}" for i in range(MAX_TAGS_PER_CALL + 1)]
        result = json.loads(apply_tags_impl(_FakeApi(), "K", too_many, dry_run=False))  # type: ignore[arg-type]
        assert "error" in result
        assert "exceeds" in result["error"]

    def test_dry_run_returns_preview_without_writing(self) -> None:
        api = _FakeApi(snapshot=ItemSnapshot(item_key="K", version=5, tags=["existing"], raw={}))
        result = json.loads(
            apply_tags_impl(api, "K", ["new", "existing"], dry_run=True)  # type: ignore[arg-type]
        )
        assert result["status"] == "preview"
        assert result["would_add"] == ["new"]
        assert result["already_present"] == ["existing"]
        assert result["after_apply"] == ["existing", "new"]
        assert api.set_tags_calls == []

    def test_apply_writes_union(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=["a"], raw={}),
            new_version=6,
        )
        result = json.loads(
            apply_tags_impl(api, "K", ["b", "c"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "applied"
        assert result["new_version"] == 6
        assert result["added_count"] == 2
        assert api.set_tags_calls == [("K", ["a", "b", "c"], 5)]

    def test_apply_skips_write_when_no_new_tags(self) -> None:
        api = _FakeApi(snapshot=ItemSnapshot(item_key="K", version=5, tags=["a", "b"], raw={}))
        result = json.loads(
            apply_tags_impl(api, "K", ["a", "b"], dry_run=False)  # type: ignore[arg-type]
        )
        assert result["status"] == "no_changes"
        assert api.set_tags_calls == []

    def test_missing_credentials_returns_error(self) -> None:
        api = _FakeApi(get_error=MissingCredentialsError("set creds"))
        result = json.loads(apply_tags_impl(api, "K", ["x"], dry_run=False))  # type: ignore[arg-type]
        assert "error" in result
        assert "creds" in result["error"]

    def test_version_conflict_returns_hint(self) -> None:
        api = _FakeApi(
            snapshot=ItemSnapshot(item_key="K", version=5, tags=[], raw={}),
            set_error=VersionConflictError("conflict"),
        )
        result = json.loads(apply_tags_impl(api, "K", ["x"], dry_run=False))  # type: ignore[arg-type]
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
