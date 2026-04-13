"""Data models for source code indexing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CodeRepo:
    """Represents an indexed source code repository."""

    name: str
    path: str
    repo_id: int = 0
    indexed_at: str = ""


@dataclass
class CodeSymbol:
    """A single code symbol extracted from a source file."""

    name: str
    symbol_type: str  # e.g. function_definition, class_definition
    text: str  # full source text of the symbol
    start_line: int
    end_line: int
    language: str
    file_id: int = 0
    signature: str = ""  # first line / declaration
    parent_name: str = ""  # enclosing symbol name (e.g. class name)


@dataclass
class ExtractedCodeFile:
    """Result of parsing a source file."""

    path: str
    rel_path: str
    language: str
    full_text: str = ""
    symbols: list[CodeSymbol] = field(default_factory=list)
    error: str | None = None
