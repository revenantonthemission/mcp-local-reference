"""Tests for the TreeSitter code parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_mcp.parser import TreeSitterParser, supported_extensions

from .conftest import SAMPLE_JAVASCRIPT, SAMPLE_MARKDOWN, SAMPLE_PYTHON

# tree-sitter-language-pack is optional; some tests require it
_has_tree_sitter = TreeSitterParser().is_available
requires_tree_sitter = pytest.mark.skipif(
    not _has_tree_sitter,
    reason="tree-sitter-language-pack not installed",
)


class TestSupportedExtensions:
    def test_includes_code_extensions(self) -> None:
        exts = supported_extensions()
        assert ".py" in exts
        assert ".js" in exts
        assert ".go" in exts
        assert ".rs" in exts

    def test_includes_doc_extensions(self) -> None:
        exts = supported_extensions()
        assert ".md" in exts
        assert ".rst" in exts
        assert ".txt" in exts


class TestCanParse:
    def test_python_file(self, parser: TreeSitterParser) -> None:
        assert parser.can_parse(Path("main.py")) is True

    def test_javascript_file(self, parser: TreeSitterParser) -> None:
        assert parser.can_parse(Path("app.js")) is True

    def test_typescript_file(self, parser: TreeSitterParser) -> None:
        assert parser.can_parse(Path("index.ts")) is True

    def test_markdown_is_not_code(self, parser: TreeSitterParser) -> None:
        assert parser.can_parse(Path("README.md")) is False

    def test_unsupported_extension(self, parser: TreeSitterParser) -> None:
        assert parser.can_parse(Path("data.csv")) is False


class TestIsDocFile:
    def test_markdown(self, parser: TreeSitterParser) -> None:
        assert parser.is_doc_file(Path("README.md")) is True

    def test_rst(self, parser: TreeSitterParser) -> None:
        assert parser.is_doc_file(Path("guide.rst")) is True

    def test_txt(self, parser: TreeSitterParser) -> None:
        assert parser.is_doc_file(Path("notes.txt")) is True

    def test_readme_without_extension(self, parser: TreeSitterParser) -> None:
        assert parser.is_doc_file(Path("README")) is True

    def test_changelog_without_extension(self, parser: TreeSitterParser) -> None:
        assert parser.is_doc_file(Path("CHANGELOG")) is True

    def test_python_file_is_not_doc(self, parser: TreeSitterParser) -> None:
        assert parser.is_doc_file(Path("main.py")) is False


class TestParsePythonFile:
    @requires_tree_sitter
    def test_extracts_function(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "sample.py"
        f.write_text(SAMPLE_PYTHON)
        symbols = parser.parse_file(f)

        func_names = [s.name for s in symbols if "function" in s.symbol_type]
        assert "hello" in func_names

    @requires_tree_sitter
    def test_extracts_class(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "sample.py"
        f.write_text(SAMPLE_PYTHON)
        symbols = parser.parse_file(f)

        class_names = [s.name for s in symbols if "class" in s.symbol_type]
        assert "Calculator" in class_names

    @requires_tree_sitter
    def test_extracts_methods_with_parent(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "sample.py"
        f.write_text(SAMPLE_PYTHON)
        symbols = parser.parse_file(f)

        methods = [s for s in symbols if s.parent_name == "Calculator"]
        method_names = {s.name for s in methods}
        assert "add" in method_names
        assert "subtract" in method_names

    def test_symbols_have_line_numbers(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "sample.py"
        f.write_text(SAMPLE_PYTHON)
        symbols = parser.parse_file(f)

        for s in symbols:
            assert s.start_line >= 1
            assert s.end_line >= s.start_line

    def test_symbols_have_language(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "sample.py"
        f.write_text(SAMPLE_PYTHON)
        symbols = parser.parse_file(f)

        for s in symbols:
            assert s.language == "python"

    def test_symbols_have_signatures(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "sample.py"
        f.write_text(SAMPLE_PYTHON)
        symbols = parser.parse_file(f)

        for s in symbols:
            assert s.signature  # non-empty


class TestParseJavaScriptFile:
    @requires_tree_sitter
    def test_extracts_function(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "app.js"
        f.write_text(SAMPLE_JAVASCRIPT)
        symbols = parser.parse_file(f)

        names = [s.name for s in symbols]
        assert "greet" in names

    @requires_tree_sitter
    def test_extracts_class(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "app.js"
        f.write_text(SAMPLE_JAVASCRIPT)
        symbols = parser.parse_file(f)

        class_names = [s.name for s in symbols if "class" in s.symbol_type]
        assert "Counter" in class_names


class TestParseDocFile:
    def test_small_doc_returns_single_symbol(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "README.md"
        f.write_text(SAMPLE_MARKDOWN)
        symbols = parser.parse_file(f)

        # Small doc (< max_doc_chunk_lines) → single file-level symbol
        assert len(symbols) >= 1
        assert symbols[0].language == "text"

    def test_large_doc_split_by_headings(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        # Generate a doc that exceeds max_doc_chunk_lines (200)
        sections = []
        for i in range(10):
            sections.append(f"# Section {i}\n\n")
            sections.append("Lorem ipsum dolor sit amet.\n" * 25)

        f = tmp_dir / "large.md"
        f.write_text("\n".join(sections))
        symbols = parser.parse_file(f)

        # Should be split into multiple sections
        assert len(symbols) > 1
        types = {s.symbol_type for s in symbols}
        assert "doc_section" in types


class TestParseEdgeCases:
    def test_empty_file_returns_no_symbols(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "empty.py"
        f.write_text("")
        assert parser.parse_file(f) == []

    def test_whitespace_only_returns_no_symbols(
        self, parser: TreeSitterParser, tmp_dir: Path
    ) -> None:
        f = tmp_dir / "blank.py"
        f.write_text("   \n\n  \n")
        assert parser.parse_file(f) == []

    def test_nonexistent_file_returns_empty(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        f = tmp_dir / "missing.py"
        assert parser.parse_file(f) == []

    def test_file_without_symbols_falls_back(self, parser: TreeSitterParser, tmp_dir: Path) -> None:
        """A Python file with only comments should fall back to file-level symbol."""
        f = tmp_dir / "comments.py"
        f.write_text("# Just a comment\nx = 42\n")
        symbols = parser.parse_file(f)

        # Should have at least one symbol (fallback to file-level)
        assert len(symbols) >= 1


class TestFallbackParsing:
    """Test the file-level fallback when tree-sitter isn't available."""

    def test_file_level_symbol_covers_whole_file(self, tmp_dir: Path) -> None:
        parser = TreeSitterParser()
        # Force tree-sitter unavailable
        parser._available = False

        f = tmp_dir / "module.py"
        f.write_text(SAMPLE_PYTHON)
        symbols = parser.parse_file(f)

        assert len(symbols) == 1
        assert symbols[0].symbol_type == "file"
        assert symbols[0].name == "module"
        assert symbols[0].start_line == 1
        assert symbols[0].language == "python"
