"""Configuration for the MCP Local Reference server."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path


def _default_zotero_dir() -> Path:
    """Detect the default Zotero data directory for the current platform."""
    system = platform.system()
    home = Path.home()

    if system == "Darwin":
        return home / "Zotero"
    elif system == "Windows":
        return home / "Zotero"
    else:  # Linux and others
        return home / "Zotero"


@dataclass
class Config:
    """Server configuration, populated from environment variables with sensible defaults."""

    zotero_data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("ZOTERO_DATA_DIR", str(_default_zotero_dir())))
    )
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("MCP_DATA_DIR", str(Path.home() / ".mcp-local-reference"))
        )
    )
    figure_dpi: int = field(default_factory=lambda: int(os.environ.get("FIGURE_DPI", "300")))
    min_figure_pixels: int = field(
        default_factory=lambda: int(os.environ.get("MIN_FIGURE_PIXELS", "10000"))
    )

    @property
    def zotero_db_path(self) -> Path:
        return self.zotero_data_dir / "zotero.sqlite"

    @property
    def zotero_storage_dir(self) -> Path:
        return self.zotero_data_dir / "storage"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    def validate(self) -> None:
        """Raise FileNotFoundError if the Zotero database cannot be found."""
        if not self.zotero_db_path.exists():
            raise FileNotFoundError(
                f"Zotero database not found at {self.zotero_db_path}. "
                "Set the ZOTERO_DATA_DIR environment variable to your Zotero data directory."
            )
