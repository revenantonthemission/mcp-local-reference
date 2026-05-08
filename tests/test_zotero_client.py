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
        assert len(refs) == 5


class TestGetItemCollections:
    def test_returns_keys_for_item_in_one_collection(self, config: Config) -> None:
        from mcp_local_reference.services.zotero_client import ZoteroClient

        client = ZoteroClient(config)
        # TESTKEY1 is in COLL1 per conftest seed data.
        keys = client.get_item_collections("TESTKEY1")
        assert keys == ["COLL1"]

    def test_returns_empty_for_unfiled_item(self, config: Config) -> None:
        from mcp_local_reference.services.zotero_client import ZoteroClient

        client = ZoteroClient(config)
        keys = client.get_item_collections("NOSUCHKK")
        assert keys == []


class TestCountItemsPerCollection:
    def test_returns_count_for_each_collection(self, config: Config) -> None:
        client = ZoteroClient(config)
        counts = client.count_items_per_collection()
        # COLL1 has TESTKEY1 and TESTKEY2 per conftest seed data.
        assert counts.get("COLL1") == 2


class TestFindByDoi:
    def test_find_by_doi_returns_item_key_when_match(self, config: Config) -> None:
        # DOIITEM1 (itemID=3) has DOI '10.1000/dedup.test' per conftest seed data.
        result = ZoteroClient(config).find_by_doi("10.1000/dedup.test")
        assert result == "DOIITEM1"

    def test_find_by_doi_returns_none_when_no_match(self, config: Config) -> None:
        result = ZoteroClient(config).find_by_doi("10.9999/no-such-paper")
        assert result is None

    def test_find_by_doi_ignores_deleted_items(self, config: Config) -> None:
        import sqlite3

        # Mark DOIITEM1 (itemID=3) as deleted — db is at zotero_data_dir/zotero.sqlite.
        db_path = config.zotero_data_dir / "zotero.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO deletedItems VALUES (3)")
        conn.commit()
        conn.close()

        result = ZoteroClient(config).find_by_doi("10.1000/dedup.test")
        assert result is None


class TestFindByArxivId:
    def test_via_extra_field(self, config: Config) -> None:
        """ARXITEM1 has 'arXiv:2401.99999' in its extra field."""
        client = ZoteroClient(config)
        assert client.find_by_arxiv_id("2401.99999") == "ARXITEM1"

    def test_strips_version_suffix(self, config: Config) -> None:
        """v3 of an existing paper should match the version-naked stored form."""
        client = ZoteroClient(config)
        assert client.find_by_arxiv_id("2401.99999v3") == "ARXITEM1"

    def test_via_doi_field(self, config: Config) -> None:
        """If user stored an arXiv paper by its 10.48550/arXiv.<id> DOI, match that too."""
        import sqlite3

        db_path = config.zotero_data_dir / "zotero.sqlite"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("INSERT INTO items VALUES (6, 1, 1, 'DOIARXIV1', 1)")
            conn.execute("INSERT INTO itemDataValues VALUES (30, '10.48550/arXiv.2402.55555')")
            conn.execute("INSERT INTO itemData VALUES (6, 3, 30)")
            conn.commit()

        client = ZoteroClient(config)
        assert client.find_by_arxiv_id("2402.55555") == "DOIARXIV1"

    def test_returns_none_when_no_match(self, config: Config) -> None:
        client = ZoteroClient(config)
        assert client.find_by_arxiv_id("1111.22222") is None


class TestFindByIsbn:
    def test_normalizes_both_sides(self, config: Config) -> None:
        """Stored as '978-0-674-04207-0' (hyphenated). Query unhyphenated must match."""
        client = ZoteroClient(config)
        assert client.find_by_isbn("9780674042070") == "ISBNITEM1"

    def test_matches_hyphenated_query(self, config: Config) -> None:
        client = ZoteroClient(config)
        assert client.find_by_isbn("978-0-674-04207-0") == "ISBNITEM1"

    def test_uppercase_x(self, config: Config) -> None:
        """ISBN-10 with check digit X stored lowercase must still match uppercase query."""
        import sqlite3

        db_path = config.zotero_data_dir / "zotero.sqlite"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("INSERT INTO items VALUES (7, 2, 1, 'ISBNXITEM', 1)")
            conn.execute("INSERT INTO itemDataValues VALUES (40, '0-13-602X-1')")
            conn.execute("INSERT INTO itemData VALUES (7, 13, 40)")
            conn.commit()

        client = ZoteroClient(config)
        assert client.find_by_isbn("013602X1") == "ISBNXITEM"

    def test_returns_none_when_no_match(self, config: Config) -> None:
        client = ZoteroClient(config)
        assert client.find_by_isbn("0000000000000") is None
