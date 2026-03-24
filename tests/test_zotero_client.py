"""Tests for the Zotero SQLite client."""

from __future__ import annotations

from mcp_local_reference.config import Config
from mcp_local_reference.services.zotero_client import ZoteroClient


class TestSearch:
    def test_finds_article_by_title_keyword(self, config: Config) -> None:
        results = ZoteroClient(config).search("Deep Learning")
        assert len(results) == 1
        assert results[0].title == "Deep Learning for Natural Language Processing"

    def test_finds_article_by_abstract_keyword(self, config: Config) -> None:
        results = ZoteroClient(config).search("comprehensive review")
        assert len(results) == 1

    def test_returns_empty_for_no_match(self, config: Config) -> None:
        assert ZoteroClient(config).search("quantum computing") == []

    def test_respects_limit(self, config: Config) -> None:
        results = ZoteroClient(config).search("a", limit=1)
        assert len(results) <= 1


class TestGetReference:
    def test_returns_full_metadata(self, config: Config) -> None:
        ref = ZoteroClient(config).get_reference("TESTKEY1")
        assert ref is not None
        assert ref.title == "Deep Learning for Natural Language Processing"
        assert ref.doi == "10.1234/test.2024"
        assert ref.volume == "42"
        assert ref.issue == "3"
        assert ref.pages == "100-150"

    def test_returns_creators(self, config: Config) -> None:
        ref = ZoteroClient(config).get_reference("TESTKEY1")
        assert ref is not None
        assert len(ref.creators) == 2
        assert ref.creators[0]["lastName"] == "Smith"
        assert ref.creators[1]["lastName"] == "Jones"

    def test_returns_tags(self, config: Config) -> None:
        ref = ZoteroClient(config).get_reference("TESTKEY1")
        assert ref is not None
        assert set(ref.tags) == {"deep-learning", "nlp"}

    def test_returns_none_for_missing_key(self, config: Config) -> None:
        assert ZoteroClient(config).get_reference("NONEXIST") is None

    def test_book_reference(self, config: Config) -> None:
        ref = ZoteroClient(config).get_reference("TESTKEY2")
        assert ref is not None
        assert ref.item_type == "book"
        assert "Modern Approach" in ref.title
        assert ref.publisher == "Pearson"


class TestCollections:
    def test_list_collections(self, config: Config) -> None:
        cols = ZoteroClient(config).list_collections()
        assert len(cols) == 1
        assert cols[0].name == "Machine Learning"

    def test_get_collection_items(self, config: Config) -> None:
        items = ZoteroClient(config).get_collection_items("COLL1")
        assert len(items) == 2
        keys = {r.item_key for r in items}
        assert keys == {"TESTKEY1", "TESTKEY2"}


class TestGetAllReferences:
    def test_returns_all(self, config: Config) -> None:
        refs = ZoteroClient(config).get_all_references()
        assert len(refs) == 2
