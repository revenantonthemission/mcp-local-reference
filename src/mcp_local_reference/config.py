"""Configuration for the MCP Local Reference server.

Backed by ``pydantic-settings``: every field is overridable via the
matching environment variable (case-insensitive), with a per-field
``validation_alias`` used when the env-var name doesn't follow the
field name.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Config(BaseSettings):
    """Server configuration, populated from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    zotero_data_dir: Path = Field(default_factory=lambda: Path.home() / "Zotero")
    data_dir: Path = Field(
        default_factory=lambda: Path.home() / ".mcp-local-reference",
        validation_alias="MCP_DATA_DIR",
    )
    figure_dpi: int = 300
    min_figure_pixels: int = 10_000
    local_pdf_dir: Path | None = None
    zotero_user_id: str = ""
    zotero_api_key: str = ""
    zotero_api_base_url: str = "https://api.zotero.org"
    add_reference_max_pdf_mb: int = 50

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
