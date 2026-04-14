"""Tests for the CodeIndexManager facade."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_mcp.code_fts import CodeFTSIndex
from code_mcp.code_manager import DEFAULT_EXCLUDE_PATTERNS, CodeIndexManager
from code_mcp.parser import TreeSitterParser


@pytest.fixture()
def manager(tmp_dir: Path, mock_embedder: MagicMock) -> CodeIndexManager:
    """Return a CodeIndexManager with real parser + FTS, mocked embedder."""
    db_path = tmp_dir / "manager_test.db"
    fts_index = CodeFTSIndex(db_path=db_path)
    return CodeIndexManager(
        parser=TreeSitterParser(),
        fts_index=fts_index,
        embedder=mock_embedder,
    )


class TestIndexRepo:
    def test_indexes_sample_repo(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        stats = manager.index_repo(sample_repo, skip_vectors=True)

        assert stats["total"] > 0
        assert stats["indexed"] > 0
        assert stats["symbols"] > 0
        assert stats["failed"] >= 0

    def test_skips_excluded_directories(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        manager.index_repo(sample_repo, skip_vectors=True)

        # node_modules should be excluded
        repos = manager.list_repos()
        assert len(repos) == 1

        symbols = manager.fts_index.get_symbols_by_file("sample-repo", "node_modules/pkg.js")
        assert symbols == []

    def test_skips_empty_files(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        manager.index_repo(sample_repo, skip_vectors=True)

        # empty.py has no content, should not be indexed
        symbols = manager.fts_index.get_symbols_by_file("sample-repo", "empty.py")
        assert symbols == []

    def test_indexes_subdirectory_files(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        manager.index_repo(sample_repo, skip_vectors=True)

        symbols = manager.fts_index.get_symbols_by_file("sample-repo", "lib/utils.py")
        assert len(symbols) >= 1

    def test_change_detection_skips_unchanged(
        self, manager: CodeIndexManager, sample_repo: Path
    ) -> None:
        stats1 = manager.index_repo(sample_repo, skip_vectors=True)
        stats2 = manager.index_repo(sample_repo, skip_vectors=True)

        # Second run should skip everything (files unchanged)
        assert stats2["skipped"] == stats1["indexed"] + stats1["skipped"]
        assert stats2["indexed"] == 0

    def test_rebuild_forces_reindex(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        manager.index_repo(sample_repo, skip_vectors=True)
        stats = manager.index_repo(sample_repo, skip_vectors=True, rebuild=True)

        # Rebuild should re-index everything
        assert stats["indexed"] > 0
        assert stats["skipped"] == 0

    def test_limit_caps_files(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        stats = manager.index_repo(sample_repo, skip_vectors=True, limit=1)
        assert stats["total"] == 1

    def test_custom_repo_name(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        manager.index_repo(sample_repo, name="custom-name", skip_vectors=True)
        repos = manager.list_repos()
        assert repos[0]["name"] == "custom-name"

    def test_nonexistent_repo_raises(self, manager: CodeIndexManager, tmp_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            manager.index_repo(tmp_dir / "nonexistent", skip_vectors=True)


class TestKeywordSearch:
    @pytest.fixture(autouse=True)
    def _indexed(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        manager.index_repo(sample_repo, skip_vectors=True)

    def test_finds_by_keyword(self, manager: CodeIndexManager) -> None:
        # Search for text content (works with or without tree-sitter)
        results = manager.keyword_search("Calculator")
        assert len(results) >= 1

    def test_empty_query_returns_empty(self, manager: CodeIndexManager) -> None:
        assert manager.keyword_search("") == []
        assert manager.keyword_search("   ") == []

    def test_filters_by_repo(self, manager: CodeIndexManager) -> None:
        results = manager.keyword_search("hello", repos=["sample-repo"])
        assert len(results) >= 1

    def test_nonexistent_repo_filter_returns_empty(self, manager: CodeIndexManager) -> None:
        results = manager.keyword_search("hello", repos=["nonexistent"])
        assert results == []

    def test_filters_by_language(self, manager: CodeIndexManager) -> None:
        results = manager.keyword_search("hello", languages=["python"])
        for r in results:
            assert r["language"] == "python"

    def test_results_enriched_with_metadata(self, manager: CodeIndexManager) -> None:
        results = manager.keyword_search("hello")
        if results:
            r = results[0]
            assert "repo_name" in r
            assert "rel_path" in r
            assert "source" in r
            assert r["source"] == "keyword"


class TestSemanticSearch:
    def test_returns_empty_when_unavailable(
        self, manager: CodeIndexManager, sample_repo: Path
    ) -> None:
        """Semantic search returns empty when embedder is unavailable."""
        manager.index_repo(sample_repo, skip_vectors=True)
        results = manager.semantic_search("hello")
        assert results == []


class TestFileExclusion:
    def test_hidden_files_excluded(self, manager: CodeIndexManager) -> None:
        assert manager._should_exclude(Path("/repo/.hidden/file.py"), Path("/repo")) is True

    def test_node_modules_excluded(self, manager: CodeIndexManager) -> None:
        assert (
            manager._should_exclude(Path("/repo/node_modules/pkg/index.js"), Path("/repo")) is True
        )

    def test_pycache_excluded(self, manager: CodeIndexManager) -> None:
        assert (
            manager._should_exclude(Path("/repo/__pycache__/mod.cpython-311.pyc"), Path("/repo"))
            is True
        )

    def test_regular_file_not_excluded(self, manager: CodeIndexManager) -> None:
        assert manager._should_exclude(Path("/repo/src/main.py"), Path("/repo")) is False

    def test_all_default_patterns_excluded(self, manager: CodeIndexManager) -> None:
        for pattern in DEFAULT_EXCLUDE_PATTERNS:
            path = Path(f"/repo/{pattern}/file.py")
            assert manager._should_exclude(path, Path("/repo")) is True, (
                f"{pattern} should be excluded"
            )


class TestReindexFile:
    def test_reindexes_single_file(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        result = manager.reindex_file("sample-repo", sample_repo, sample_repo / "main.py")
        assert result == "indexed"

    def test_skips_unchanged_file(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        manager.reindex_file("sample-repo", sample_repo, sample_repo / "main.py")
        result = manager.reindex_file("sample-repo", sample_repo, sample_repo / "main.py")
        assert result == "skipped"


class TestRemoveFileByPath:
    def test_removes_indexed_file(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        manager.index_repo(sample_repo, skip_vectors=True)
        removed = manager.remove_file_by_path("sample-repo", "main.py")
        assert removed is True

        symbols = manager.fts_index.get_symbols_by_file("sample-repo", "main.py")
        assert symbols == []

    def test_returns_false_for_missing_file(self, manager: CodeIndexManager) -> None:
        assert manager.remove_file_by_path("ghost", "ghost.py") is False


class TestGetStats:
    def test_includes_fts_and_vector_stats(
        self, manager: CodeIndexManager, sample_repo: Path
    ) -> None:
        manager.index_repo(sample_repo, skip_vectors=True)
        stats = manager.get_stats()

        assert "repo_count" in stats
        assert "file_count" in stats
        assert "symbol_count" in stats
        assert "index_size_mb" in stats
        assert "vector_index_size_mb" in stats
        assert "total_index_size_mb" in stats
        assert stats["repo_count"] == 1
        assert stats["file_count"] > 0


class TestRemoveRepo:
    def test_removes_repo(self, manager: CodeIndexManager, sample_repo: Path) -> None:
        manager.index_repo(sample_repo, skip_vectors=True)
        manager.remove_repo("sample-repo")
        assert manager.list_repos() == []


class TestMtimeSkipping:
    def test_second_run_uses_mtime_to_skip(
        self, manager: CodeIndexManager, sample_repo: Path
    ) -> None:
        """Second indexing run should skip unchanged files via mtime (no file reads)."""
        stats1 = manager.index_repo(sample_repo, skip_vectors=True)
        assert stats1["indexed"] > 0

        stats2 = manager.index_repo(sample_repo, skip_vectors=True)
        # All files skipped on second run
        assert stats2["skipped"] == stats1["total"]
        assert stats2["indexed"] == 0

    def test_modified_file_gets_reindexed(
        self, manager: CodeIndexManager, sample_repo: Path
    ) -> None:
        """A file with changed mtime and content should be re-indexed."""
        manager.index_repo(sample_repo, skip_vectors=True)

        # Modify a file (changes both mtime and hash)
        target = sample_repo / "main.py"
        time.sleep(0.05)  # ensure mtime changes
        target.write_text("def new_func():\n    return 'changed'\n")

        stats = manager.index_repo(sample_repo, skip_vectors=True)
        assert stats["indexed"] >= 1

    def test_touched_file_with_same_content_skipped(
        self, manager: CodeIndexManager, sample_repo: Path
    ) -> None:
        """A file whose mtime changed but content is identical should be skipped."""
        manager.index_repo(sample_repo, skip_vectors=True)

        # Touch file to change mtime without changing content
        target = sample_repo / "main.py"
        content = target.read_text()
        time.sleep(0.05)
        target.write_text(content)

        stats = manager.index_repo(sample_repo, skip_vectors=True)
        # The touched file should be skipped (hash matches), others skipped by mtime
        assert stats["indexed"] == 0
