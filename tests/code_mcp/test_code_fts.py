"""Tests for the SQLite FTS5 code index."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_mcp.code_fts import CodeFTSIndex

from .conftest import make_symbol


class TestUpsertRepo:
    def test_creates_repo(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("test-repo", Path("/tmp/test-repo"))
        assert repo_id > 0

    def test_returns_same_id_on_duplicate(self, fts_index: CodeFTSIndex) -> None:
        id1 = fts_index.upsert_repo("test-repo", Path("/tmp/test-repo"))
        id2 = fts_index.upsert_repo("test-repo", Path("/tmp/test-repo"))
        assert id1 == id2

    def test_updates_path_on_conflict(self, fts_index: CodeFTSIndex) -> None:
        fts_index.upsert_repo("test-repo", Path("/old/path"))
        fts_index.upsert_repo("test-repo", Path("/new/path"))
        repos = fts_index.list_repos()
        assert repos[0]["path"] == "/new/path"


class TestUpsertFile:
    def test_creates_file(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        file_id = fts_index.upsert_file(repo_id, "main.py", "python", 1024, "abc123")
        assert file_id > 0

    def test_returns_new_id_on_update(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        id1 = fts_index.upsert_file(repo_id, "main.py", "python", 1024, "hash1")
        id2 = fts_index.upsert_file(repo_id, "main.py", "python", 2048, "hash2")
        assert id1 == id2  # same file, same id

    def test_clears_old_symbols_on_upsert(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        file_id = fts_index.upsert_file(repo_id, "main.py", "python", 100, "h1")
        fts_index.add_symbols(file_id, [make_symbol(name="old_func")])

        # Re-upsert same file
        fts_index.upsert_file(repo_id, "main.py", "python", 100, "h2")

        # Old symbols should be gone
        symbols = fts_index.get_symbols_by_file("repo", "main.py")
        assert len(symbols) == 0


class TestAddSymbols:
    def test_adds_symbols(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        file_id = fts_index.upsert_file(repo_id, "main.py", "python", 100, "h1")

        symbols = [
            make_symbol(name="func_a", text="def func_a():\n    return 1"),
            make_symbol(name="func_b", text="def func_b():\n    return 2"),
        ]
        fts_index.add_symbols(file_id, symbols)

        stored = fts_index.get_symbols_by_file("repo", "main.py")
        assert len(stored) == 2
        names = {s["symbol_name"] for s in stored}
        assert names == {"func_a", "func_b"}

    def test_empty_symbols_is_noop(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        file_id = fts_index.upsert_file(repo_id, "main.py", "python", 100, "h1")
        fts_index.add_symbols(file_id, [])
        assert fts_index.get_symbols_by_file("repo", "main.py") == []


class TestSearch:
    @pytest.fixture(autouse=True)
    def _seed_data(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("myrepo", Path("/tmp/myrepo"))
        file_id = fts_index.upsert_file(repo_id, "calc.py", "python", 500, "h1")
        fts_index.add_symbols(
            file_id,
            [
                make_symbol(
                    name="add_numbers",
                    text="def add_numbers(a, b):\n    return a + b",
                    signature="def add_numbers(a, b):",
                ),
                make_symbol(
                    name="multiply",
                    text="def multiply(a, b):\n    return a * b",
                    signature="def multiply(a, b):",
                ),
            ],
        )

        file_id2 = fts_index.upsert_file(repo_id, "greet.js", "javascript", 200, "h2")
        fts_index.add_symbols(
            file_id2,
            [
                make_symbol(
                    name="greet",
                    text="function greet(name) { return 'Hello ' + name; }",
                    language="javascript",
                    symbol_type="function_declaration",
                ),
            ],
        )

    def test_finds_by_symbol_name(self, fts_index: CodeFTSIndex) -> None:
        results = fts_index.search("add_numbers")
        assert len(results) >= 1
        assert results[0].symbol_name == "add_numbers"

    def test_finds_by_text_content(self, fts_index: CodeFTSIndex) -> None:
        results = fts_index.search("multiply")
        assert len(results) >= 1
        names = {r.symbol_name for r in results}
        assert "multiply" in names

    def test_returns_empty_for_no_match(self, fts_index: CodeFTSIndex) -> None:
        results = fts_index.search("nonexistent_function_xyz")
        assert results == []

    def test_respects_limit(self, fts_index: CodeFTSIndex) -> None:
        results = fts_index.search("def", limit=1)
        assert len(results) <= 1

    def test_filters_by_repo_ids(self, fts_index: CodeFTSIndex) -> None:
        # Add another repo
        repo_id2 = fts_index.upsert_repo("other", Path("/tmp/other"))
        file_id = fts_index.upsert_file(repo_id2, "other.py", "python", 100, "h3")
        fts_index.add_symbols(
            file_id,
            [make_symbol(name="add_numbers", text="def add_numbers(): pass")],
        )

        # Filter to only "other" repo
        results = fts_index.search("add_numbers", repo_ids=[repo_id2])
        for r in results:
            # All results should be from the "other" repo's file
            assert r.file_id == file_id

    def test_filters_by_language(self, fts_index: CodeFTSIndex) -> None:
        results = fts_index.search("greet", languages={"javascript"})
        assert len(results) >= 1
        for r in results:
            assert r.language == "javascript"

    def test_results_have_scores(self, fts_index: CodeFTSIndex) -> None:
        results = fts_index.search("add_numbers")
        for r in results:
            assert r.score > 0

    def test_results_have_highlights(self, fts_index: CodeFTSIndex) -> None:
        results = fts_index.search("multiply")
        for r in results:
            assert isinstance(r.highlights, str)


class TestNeedsIndexing:
    def test_new_file_needs_indexing(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        assert fts_index.needs_indexing(repo_id, "new.py", "abc") is True

    def test_unchanged_file_skipped(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "main.py", "python", 100, "samehash")
        assert fts_index.needs_indexing(repo_id, "main.py", "samehash") is False

    def test_changed_file_needs_reindex(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "main.py", "python", 100, "oldhash")
        assert fts_index.needs_indexing(repo_id, "main.py", "newhash") is True


class TestListRepos:
    def test_empty_index(self, fts_index: CodeFTSIndex) -> None:
        assert fts_index.list_repos() == []

    def test_lists_repos_with_counts(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("myrepo", Path("/tmp/myrepo"))
        file_id = fts_index.upsert_file(repo_id, "f.py", "python", 100, "h")
        fts_index.add_symbols(file_id, [make_symbol()])

        repos = fts_index.list_repos()
        assert len(repos) == 1
        assert repos[0]["name"] == "myrepo"
        assert repos[0]["file_count"] == 1
        assert repos[0]["symbol_count"] == 1


class TestRemoveRepo:
    def test_removes_repo_and_data(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("myrepo", Path("/tmp/myrepo"))
        file_id = fts_index.upsert_file(repo_id, "f.py", "python", 100, "h")
        fts_index.add_symbols(file_id, [make_symbol()])

        fts_index.remove_repo("myrepo")

        assert fts_index.list_repos() == []
        assert fts_index.get_symbols_by_file("myrepo", "f.py") == []

    def test_remove_nonexistent_is_noop(self, fts_index: CodeFTSIndex) -> None:
        fts_index.remove_repo("ghost")  # should not raise


class TestGetStats:
    def test_empty_stats(self, fts_index: CodeFTSIndex) -> None:
        stats = fts_index.get_stats()
        assert stats["repo_count"] == 0
        assert stats["file_count"] == 0
        assert stats["symbol_count"] == 0

    def test_stats_after_indexing(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        file_id = fts_index.upsert_file(repo_id, "f.py", "python", 100, "h")
        fts_index.add_symbols(file_id, [make_symbol(), make_symbol(name="other")])

        stats = fts_index.get_stats()
        assert stats["repo_count"] == 1
        assert stats["file_count"] == 1
        assert stats["symbol_count"] == 2
        assert stats["index_size_mb"] >= 0


class TestBatchInsert:
    def test_batch_adds_files_and_symbols(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))

        files_data = [
            ("a.py", "python", 100, "ha", None, [make_symbol(name="func_a")]),
            ("b.py", "python", 200, "hb", None, [make_symbol(name="func_b")]),
        ]
        results = fts_index.add_files_batch(repo_id, files_data)

        assert len(results) == 2
        assert fts_index.get_stats()["file_count"] == 2
        assert fts_index.get_stats()["symbol_count"] == 2

    def test_batch_populates_fts(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))

        files_data = [
            (
                "calc.py",
                "python",
                100,
                "hc",
                None,
                [make_symbol(name="batch_add", text="def batch_add(x, y): return x + y")],
            ),
        ]
        fts_index.add_files_batch(repo_id, files_data)

        # FTS should find the symbol
        results = fts_index.search("batch_add")
        assert len(results) >= 1

    def test_batch_empty_is_noop(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        results = fts_index.add_files_batch(repo_id, [])
        assert results == []


class TestGetSymbolByName:
    def test_returns_matching_symbols(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        file_id = fts_index.upsert_file(repo_id, "m.py", "python", 100, "h")
        fts_index.add_symbols(
            file_id,
            [
                make_symbol(name="my_func", text="def my_func(): pass"),
                make_symbol(name="other", text="def other(): pass"),
            ],
        )

        results = fts_index.get_symbol_by_name("repo", "m.py", "my_func")
        assert len(results) == 1
        assert results[0]["symbol_name"] == "my_func"

    def test_returns_empty_for_no_match(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "m.py", "python", 100, "h")
        assert fts_index.get_symbol_by_name("repo", "m.py", "ghost") == []


class TestPersistentConnection:
    def test_connect_returns_same_connection(self, fts_index: CodeFTSIndex) -> None:
        with fts_index._connect() as conn1:
            pass
        with fts_index._connect() as conn2:
            pass
        assert conn1 is conn2

    def test_close_closes_connection(self, fts_index: CodeFTSIndex) -> None:
        fts_index.close()
        # After close, a new connection should be created
        with fts_index._connect() as conn:
            # Should work fine — new connection created
            row = conn.execute("SELECT 1").fetchone()
            assert row[0] == 1


class TestMtimeAndBulkHash:
    def test_upsert_file_stores_mtime(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "main.py", "python", 1024, "abc123", file_mtime=1000.5)

        hashes = fts_index.get_all_file_hashes(repo_id)
        assert "main.py" in hashes
        assert hashes["main.py"] == ("abc123", 1000.5)

    def test_upsert_file_mtime_defaults_to_none(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "main.py", "python", 1024, "abc123")

        hashes = fts_index.get_all_file_hashes(repo_id)
        assert hashes["main.py"] == ("abc123", None)

    def test_get_all_file_hashes_returns_all_files(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "a.py", "python", 100, "h1", file_mtime=1.0)
        fts_index.upsert_file(repo_id, "b.py", "python", 200, "h2", file_mtime=2.0)

        hashes = fts_index.get_all_file_hashes(repo_id)
        assert len(hashes) == 2
        assert hashes["a.py"] == ("h1", 1.0)
        assert hashes["b.py"] == ("h2", 2.0)

    def test_get_all_file_hashes_empty_repo(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        hashes = fts_index.get_all_file_hashes(repo_id)
        assert hashes == {}

    def test_get_all_file_hashes_ignores_other_repos(self, fts_index: CodeFTSIndex) -> None:
        r1 = fts_index.upsert_repo("repo1", Path("/tmp/repo1"))
        r2 = fts_index.upsert_repo("repo2", Path("/tmp/repo2"))
        fts_index.upsert_file(r1, "a.py", "python", 100, "h1", file_mtime=1.0)
        fts_index.upsert_file(r2, "b.py", "python", 200, "h2", file_mtime=2.0)

        hashes = fts_index.get_all_file_hashes(r1)
        assert len(hashes) == 1
        assert "a.py" in hashes

    def test_update_mtimes_batch(self, fts_index: CodeFTSIndex) -> None:
        repo_id = fts_index.upsert_repo("repo", Path("/tmp/repo"))
        fts_index.upsert_file(repo_id, "a.py", "python", 100, "h1", file_mtime=1.0)
        fts_index.upsert_file(repo_id, "b.py", "python", 200, "h2", file_mtime=2.0)

        fts_index.update_mtimes_batch(repo_id, [("a.py", 10.0), ("b.py", 20.0)])

        hashes = fts_index.get_all_file_hashes(repo_id)
        assert hashes["a.py"] == ("h1", 10.0)
        assert hashes["b.py"] == ("h2", 20.0)


class TestComputeFileHash:
    def test_consistent_hash(self, tmp_dir: Path) -> None:
        f = tmp_dir / "test.py"
        f.write_text("hello world")
        h1 = CodeFTSIndex.compute_file_hash(f)
        h2 = CodeFTSIndex.compute_file_hash(f)
        assert h1 == h2

    def test_different_content_different_hash(self, tmp_dir: Path) -> None:
        f1 = tmp_dir / "a.py"
        f2 = tmp_dir / "b.py"
        f1.write_text("content a")
        f2.write_text("content b")
        assert CodeFTSIndex.compute_file_hash(f1) != CodeFTSIndex.compute_file_hash(f2)
