"""Shared fixtures for code_mcp tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_mcp.code_fts import CodeFTSIndex
from code_mcp.config import Settings
from code_mcp.models import CodeSymbol
from code_mcp.parser import TreeSitterParser

# ------------------------------------------------------------------
# Directories
# ------------------------------------------------------------------


@pytest.fixture()
def tmp_dir() -> Path:  # type: ignore[misc]
    with tempfile.TemporaryDirectory() as d:
        # Resolve symlinks (macOS /var → /private/var) to avoid
        # relative_to() failures when code calls .resolve()
        yield Path(d).resolve()


# ------------------------------------------------------------------
# Sample source files
# ------------------------------------------------------------------

SAMPLE_PYTHON = '''\
"""Module docstring."""

def hello(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}!"


class Calculator:
    """A simple calculator."""

    def add(self, a: int, b: int) -> int:
        return a + b

    def subtract(self, a: int, b: int) -> int:
        return a - b
'''

SAMPLE_JAVASCRIPT = """\
function greet(name) {
    return `Hello, ${name}!`;
}

class Counter {
    constructor() {
        this.count = 0;
    }

    increment() {
        this.count++;
    }
}
"""

SAMPLE_MARKDOWN = """\
# Project README

This is a sample project.

## Installation

Run `pip install .` to install.

## Usage

Import the module and call `hello()`.
"""

SAMPLE_EMPTY = ""
SAMPLE_WHITESPACE = "   \n\n  \n"


@pytest.fixture()
def sample_repo(tmp_dir: Path) -> Path:
    """Create a mock source code repository with sample files."""
    repo = tmp_dir / "sample-repo"
    repo.mkdir()

    # Python file
    (repo / "main.py").write_text(SAMPLE_PYTHON)

    # JavaScript file
    (repo / "app.js").write_text(SAMPLE_JAVASCRIPT)

    # Markdown doc
    (repo / "README.md").write_text(SAMPLE_MARKDOWN)

    # Empty file (should be skipped)
    (repo / "empty.py").write_text(SAMPLE_EMPTY)

    # Subdirectory with file
    sub = repo / "lib"
    sub.mkdir()
    (sub / "utils.py").write_text("def helper():\n    return 42\n")

    # Excluded directory
    excluded = repo / "node_modules"
    excluded.mkdir()
    (excluded / "pkg.js").write_text("function noop() {}")

    # Hidden file (should be excluded)
    (repo / ".secret.py").write_text("SECRET = 'oops'")

    return repo


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------


@pytest.fixture()
def code_settings(tmp_dir: Path) -> Settings:
    """Return Settings pointing at temp directories."""
    return Settings(
        repos_dir=tmp_dir,
        data_dir=tmp_dir / "code-mcp-data",
        watch_enabled=False,
    )


# ------------------------------------------------------------------
# FTS Index
# ------------------------------------------------------------------


@pytest.fixture()
def fts_index(tmp_dir: Path) -> CodeFTSIndex:
    """Return a CodeFTSIndex backed by a temp database."""
    db_path = tmp_dir / "test_code_index.db"
    return CodeFTSIndex(db_path=db_path)


# ------------------------------------------------------------------
# Parser
# ------------------------------------------------------------------


@pytest.fixture()
def parser() -> TreeSitterParser:
    """Return a fresh TreeSitterParser."""
    return TreeSitterParser()


# ------------------------------------------------------------------
# Mock embedder
# ------------------------------------------------------------------


@pytest.fixture()
def mock_embedder() -> MagicMock:
    """Return a mock CodeEmbedder with is_available=False."""
    embedder = MagicMock()
    embedder.is_available = False
    embedder.add_symbols.return_value = None
    embedder.add_symbols_batch.return_value = 0
    embedder.remove_file.return_value = None
    embedder.search.return_value = []
    embedder.get_stats.return_value = {"index_size_mb": 0.0}
    return embedder


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def make_symbol(
    name: str = "test_func",
    symbol_type: str = "function_definition",
    text: str = "def test_func():\n    pass",
    language: str = "python",
    start_line: int = 1,
    end_line: int = 2,
    **kwargs,
) -> CodeSymbol:
    """Create a CodeSymbol with sensible defaults for testing."""
    return CodeSymbol(
        name=name,
        symbol_type=symbol_type,
        text=text,
        start_line=start_line,
        end_line=end_line,
        language=language,
        **kwargs,
    )
