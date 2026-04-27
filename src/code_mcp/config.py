"""Configuration for code MCP server."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Code MCP server settings."""

    model_config = SettingsConfigDict(
        env_prefix="CODE_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Source repos directory
    repos_dir: Path = Path.home() / "source-codes"

    # Index storage
    data_dir: Path = Path.home() / ".local/share/code-mcp"

    # File filtering
    max_file_size_kb: int = 100  # skip files larger than this (KB)

    # Doc chunking
    max_doc_chunk_lines: int = 200  # split large docs by headings if above this

    # File watcher
    watch_enabled: bool = True
    watch_debounce_seconds: float = 2.0

    # Embedding settings
    embedding_model: str = "all-MiniLM-L12-v2"  # fast, good quality for code search
    embedding_batch_size: int = 256  # symbols per embedding batch
    embedding_device: str = "mps"  # auto, cpu, mps, cuda
    embedding_backend: str = "torch"  # torch, onnx, openvino

    @property
    def index_db_path(self) -> Path:
        """Get code index database path."""
        return self.data_dir / "code_index.db"

    @property
    def vector_db_path(self) -> Path:
        """Get code vector database path."""
        return self.data_dir / "code_vectors"


settings = Settings()
