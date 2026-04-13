"""Source code parser using tree-sitter for symbol extraction."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import settings
from .models import CodeSymbol

logger = logging.getLogger(__name__)

# Extension → language mapping
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".c": "c",
    ".h": "c",
    ".go": "go",
    ".py": "python",
    ".java": "java",
    ".rs": "rust",
    ".swift": "swift",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".php": "php",
}

# Language → AST node types to extract as symbols
LANGUAGE_NODE_TYPES: dict[str, set[str]] = {
    "c": {"function_definition", "struct_specifier", "enum_specifier"},
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "python": {"function_definition", "class_definition"},
    "java": {"method_declaration", "class_declaration", "interface_declaration"},
    "rust": {"function_item", "impl_item", "struct_item", "enum_item", "trait_item"},
    "swift": {
        "function_declaration",
        "class_declaration",
        "struct_declaration",
        "protocol_declaration",
    },
    "javascript": {"function_declaration", "class_declaration", "method_definition"},
    "typescript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "interface_declaration",
        "type_alias_declaration",
    },
    "tsx": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "interface_declaration",
        "type_alias_declaration",
    },
    "cpp": {
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "namespace_definition",
    },
    "csharp": {
        "method_declaration",
        "class_declaration",
        "interface_declaration",
        "struct_declaration",
    },
    "ruby": {"method", "class", "module", "singleton_method"},
    "kotlin": {
        "function_declaration",
        "class_declaration",
        "object_declaration",
    },
    "php": {
        "function_definition",
        "class_declaration",
        "method_declaration",
        "interface_declaration",
    },
}

# Container node types — these can have nested symbols (classes, modules, etc.)
# When matched, we still extract the container itself but also recurse into children.
CONTAINER_NODE_TYPES: set[str] = {
    # Python
    "class_definition",
    # Java / C# / TypeScript / JavaScript / Kotlin / PHP / Swift
    "class_declaration",
    # Rust
    "impl_item",
    # Ruby
    "class",
    "module",
    # C++ / C
    "class_specifier",
    "namespace_definition",
    # C#
    "struct_declaration",
}

# Documentation file patterns (indexed as-is, no tree-sitter)
DOC_EXTENSIONS: set[str] = {".md", ".rst", ".txt"}
DOC_FILENAMES: set[str] = {"README", "CONTRIBUTING", "CHANGELOG", "LICENSE", "AUTHORS"}


def supported_extensions() -> set[str]:
    """All file extensions supported for indexing (code + docs)."""
    return set(EXTENSION_TO_LANGUAGE.keys()) | DOC_EXTENSIONS


class TreeSitterParser:
    """Extract symbols from source code using tree-sitter.

    Falls back to file-level chunking when tree-sitter is unavailable
    or when no symbols can be extracted.
    """

    def __init__(self) -> None:
        self._parsers: dict[str, object] = {}  # lazy per-language
        self._available: bool | None = None

    @property
    def is_available(self) -> bool:
        """Check if tree-sitter-languages is importable."""
        if self._available is None:
            try:
                import tree_sitter_language_pack  # noqa: F401

                self._available = True
            except ImportError:
                self._available = False
        return self._available

    def can_parse(self, file_path: Path) -> bool:
        """Check if file extension is a supported code file."""
        return file_path.suffix.lower() in EXTENSION_TO_LANGUAGE

    def is_doc_file(self, file_path: Path) -> bool:
        """Check if file is a documentation file."""
        suffix = file_path.suffix.lower()
        if suffix in DOC_EXTENSIONS:
            return True
        return file_path.stem.upper() in DOC_FILENAMES

    def parse_file(self, file_path: Path) -> list[CodeSymbol]:
        """Parse a source file and return symbols.

        Falls back to file-level chunking if tree-sitter fails.
        """
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning(f"Cannot read {file_path}: {e}")
            return []

        if not text.strip():
            return []

        suffix = file_path.suffix.lower()
        language = EXTENSION_TO_LANGUAGE.get(suffix)

        if not language:
            # Doc file — split by sections if large enough
            return self._parse_doc_sections(file_path, text)

        # Try tree-sitter
        if self.is_available:
            symbols = self._parse_with_tree_sitter(text, language, file_path)
            if symbols:
                return symbols

        # Fallback: treat whole file as one symbol
        return self._file_level_symbol(file_path, text, language)

    def _parse_with_tree_sitter(
        self,
        text: str,
        language: str,
        file_path: Path,
    ) -> list[CodeSymbol]:
        """Parse with tree-sitter and extract top-level symbols."""
        try:
            import tree_sitter_language_pack
        except ImportError:
            return []

        try:
            parser = tree_sitter_language_pack.get_parser(language)
            tree = parser.parse(text.encode("utf-8"))
        except Exception as e:
            logger.warning(f"tree-sitter parse failed for {file_path}: {e}")
            return []

        node_types = LANGUAGE_NODE_TYPES.get(language, set())
        if not node_types:
            return []

        symbols: list[CodeSymbol] = []
        text_bytes = text.encode("utf-8")

        def _get_name(node) -> str:
            """Extract identifier name from AST node (searches up to 2 levels deep)."""
            name_types = (
                "identifier",
                "name",
                "type_identifier",
                "field_identifier",
                "simple_identifier",
                "constant",
                "namespace_identifier",
            )
            # Check direct children first
            for child in node.children:
                if child.type in name_types:
                    return child.text.decode("utf-8", errors="replace")
            # Check grandchildren (e.g. C: function_definition → function_declarator → identifier)
            for child in node.children:
                for grandchild in child.children:
                    if grandchild.type in name_types:
                        return grandchild.text.decode("utf-8", errors="replace")
            # Fallback: first line
            first_line = node.text[:80].decode("utf-8", errors="replace").split("\n")[0]
            return first_line

        def _get_signature(node) -> str:
            """Extract function/method signature (first line)."""
            return node.text.decode("utf-8", errors="replace").split("\n")[0]

        # Iterative AST walk (avoids RecursionError on deeply nested files)
        stack: list[tuple] = [(tree.root_node, "")]  # (node, parent_name)
        while stack:
            node, parent_name = stack.pop()
            if node.type in node_types:
                start_line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                symbol_text = text_bytes[node.start_byte : node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                name = _get_name(node)
                symbols.append(
                    CodeSymbol(
                        name=name,
                        symbol_type=node.type,
                        text=symbol_text,
                        start_line=start_line,
                        end_line=end_line,
                        language=language,
                        signature=_get_signature(node),
                        parent_name=parent_name,
                    )
                )
                # Container nodes: push children for nested symbol extraction
                if node.type in CONTAINER_NODE_TYPES:
                    for child in reversed(node.children):
                        stack.append((child, name))
                continue
            for child in reversed(node.children):
                stack.append((child, parent_name))
        logger.debug(f"tree-sitter: {len(symbols)} symbols from {file_path.name}")
        return symbols

    @staticmethod
    def _parse_doc_sections(file_path: Path, text: str) -> list[CodeSymbol]:
        """Split a doc file into sections by markdown headings.

        Small files (< max_doc_chunk_lines) or files without headings
        are returned as a single symbol.
        """
        lines = text.split("\n")
        line_count = len(lines)

        if line_count < settings.max_doc_chunk_lines:
            return [
                CodeSymbol(
                    name=file_path.stem,
                    symbol_type="file",
                    text=text,
                    start_line=1,
                    end_line=line_count,
                    language="text",
                    signature=file_path.name,
                )
            ]

        # Find heading boundaries (# or ##)
        heading_pattern = re.compile(r"^#{1,2}\s+(.+)$")
        sections: list[tuple[str, int, int]] = []  # (title, start_idx, end_idx)
        current_title = file_path.stem
        current_start = 0

        for i, line in enumerate(lines):
            match = heading_pattern.match(line)
            if match:
                if i > current_start:
                    sections.append((current_title, current_start, i))
                current_title = match.group(1).strip()
                current_start = i

        # Last section
        if current_start < line_count:
            sections.append((current_title, current_start, line_count))

        # If no sections found or only one section, return as single symbol
        if len(sections) <= 1:
            return [
                CodeSymbol(
                    name=file_path.stem,
                    symbol_type="file",
                    text=text,
                    start_line=1,
                    end_line=line_count,
                    language="text",
                    signature=file_path.name,
                )
            ]

        symbols: list[CodeSymbol] = []
        for title, start_idx, end_idx in sections:
            section_text = "\n".join(lines[start_idx:end_idx])
            if not section_text.strip():
                continue
            symbols.append(
                CodeSymbol(
                    name=title,
                    symbol_type="doc_section",
                    text=section_text,
                    start_line=start_idx + 1,
                    end_line=end_idx,
                    language="text",
                    signature=f"{file_path.name}: {title}",
                )
            )

        return (
            symbols
            if symbols
            else [
                CodeSymbol(
                    name=file_path.stem,
                    symbol_type="file",
                    text=text,
                    start_line=1,
                    end_line=line_count,
                    language="text",
                    signature=file_path.name,
                )
            ]
        )

    @staticmethod
    def _file_level_symbol(
        file_path: Path,
        text: str,
        language: str,
    ) -> list[CodeSymbol]:
        """Create a single symbol representing the entire file."""
        return [
            CodeSymbol(
                name=file_path.stem,
                symbol_type="file",
                text=text,
                start_line=1,
                end_line=text.count("\n") + 1,
                language=language,
                signature=file_path.name,
            )
        ]
