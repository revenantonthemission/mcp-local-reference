"""Vector embeddings for source code symbols using LanceDB."""

from __future__ import annotations

import logging

from .config import settings
from .models import CodeSymbol

logger = logging.getLogger(__name__)

MAX_EMBED_CHARS = 1500  # cap symbol text (~375 tokens, fits E5's 512-token limit)


class CodeEmbedder:
    """Vector embedder for source code symbols.

    Uses sentence-transformers for encoding and LanceDB for storage.
    """

    TABLE_NAME = "code_chunks"

    def __init__(
        self,
        model_name: str = settings.embedding_model,
        device: str = settings.embedding_device,
        backend: str = settings.embedding_backend,
    ):
        self.model_name = model_name
        self._device = device
        self._backend = backend
        self._model = None
        self._db = None
        self._embedding_dim: int | None = None

    @property
    def is_available(self) -> bool:
        """Check if sentence-transformers is importable."""
        try:
            import sentence_transformers  # noqa: F401

            return True
        except ImportError:
            return False

    @staticmethod
    def _resolve_device(device: str) -> str | None:
        """Resolve 'auto' device to the best available backend."""
        if device != "auto":
            return device
        try:
            import torch

            if torch.backends.mps.is_available():
                return "mps"
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
        return "cpu"

    @property
    def model(self):
        """Lazy-load the embedding model."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:
                raise ImportError(
                    "sentence-transformers required. Install: uv sync --extra full"
                ) from e

            resolved_device = self._resolve_device(self._device)
            backend = self._backend

            # Validate ONNX backend availability (requires optimum + onnxruntime)
            if backend == "onnx":
                try:
                    import onnxruntime  # noqa: F401
                    import optimum  # noqa: F401
                except ImportError:
                    logger.warning(
                        "ONNX backend requires optimum and onnxruntime. "
                        "Falling back to torch backend. "
                        "Install: uv sync --extra full"
                    )
                    backend = "torch"

            logger.info(
                f"Loading code embedding model: {self.model_name} "
                f"(device={resolved_device}, backend={backend})"
            )
            self._model = SentenceTransformer(
                self.model_name,
                device=resolved_device,
                backend=backend,
            )
        return self._model

    @property
    def db(self):
        """Lazy-load LanceDB connection."""
        if self._db is None:
            try:
                import lancedb
            except ImportError as e:
                raise ImportError("lancedb required. Install: uv sync --extra full") from e
            db_path = settings.vector_db_path
            db_path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(db_path))
        return self._db

    @property
    def embedding_dim(self) -> int:
        """Get embedding dimension (cached)."""
        if self._embedding_dim is None:
            dummy = self.model.encode(["test"])
            self._embedding_dim = dummy.shape[1] if len(dummy.shape) > 1 else len(dummy[0])
        return self._embedding_dim

    @property
    def is_e5(self) -> bool:
        """Check if model uses E5 asymmetric prefixes."""
        return "e5" in self.model_name.lower()

    def _embed_texts(
        self, texts: list[str], is_query: bool = False, show_progress: bool = False
    ) -> list:
        """Embed texts with optional E5 prefix."""
        if self.is_e5:
            prefix = "query: " if is_query else "passage: "
            texts = [prefix + t for t in texts]
        return self.model.encode(texts, show_progress_bar=show_progress).tolist()

    def add_symbols(self, file_id: int, symbols: list[CodeSymbol]) -> None:
        """Embed and store symbols for a file."""
        if not symbols:
            return

        # Build embedding text: name + signature + body (capped)
        texts = []
        for s in symbols:
            embed_text = f"{s.name}\n{s.signature}\n{s.text}"[:MAX_EMBED_CHARS]
            texts.append(embed_text)

        embeddings = self._embed_texts(texts, is_query=False)

        data = []
        for s, vec in zip(symbols, embeddings, strict=True):
            data.append(
                {
                    "file_id": file_id,
                    "symbol_name": s.name,
                    "symbol_type": s.symbol_type,
                    "language": s.language,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "text": s.text[:MAX_EMBED_CHARS],
                    "parent_name": s.parent_name,
                    "vector": vec,
                }
            )

        table_name = self.TABLE_NAME
        if table_name in self.db.table_names():
            table = self.db.open_table(table_name)
            table.delete(f"file_id = {file_id}")
            table.add(data)
        else:
            self.db.create_table(table_name, data)

    MIN_EMBED_CHARS = 20  # skip trivial symbols (e.g., "pass", empty stubs)

    def add_symbols_batch(
        self,
        file_symbols: list[tuple[int, list[CodeSymbol]]],
        batch_size: int | None = None,
    ) -> int:
        """Embed and store symbols from multiple files in large batches.

        Args:
            file_symbols: list of (file_id, symbols) tuples
            batch_size: symbols per embedding batch (default from settings)

        Returns:
            Total number of symbols embedded.
        """
        if not file_symbols:
            return 0

        batch_size = batch_size or settings.embedding_batch_size

        # Flatten all symbols with their file_ids, filtering trivial ones
        all_items: list[tuple[int, CodeSymbol, str]] = []
        skipped = 0
        for file_id, symbols in file_symbols:
            for s in symbols:
                embed_text = f"{s.name}\n{s.signature}\n{s.text}"[:MAX_EMBED_CHARS]
                if len(s.text.strip()) < self.MIN_EMBED_CHARS:
                    skipped += 1
                    continue
                all_items.append((file_id, s, embed_text))

        if skipped:
            logger.info(f"Skipped {skipped} trivial symbols (< {self.MIN_EMBED_CHARS} chars)")

        if not all_items:
            return 0

        total = len(all_items)
        logger.info(f"Batch embedding {total} symbols in batches of {batch_size}")

        table_name = self.TABLE_NAME

        # Process in batches
        all_data: list[dict] = []
        for batch_start in range(0, total, batch_size):
            batch = all_items[batch_start : batch_start + batch_size]
            texts = [embed_text for _, _, embed_text in batch]
            embeddings = self._embed_texts(texts, is_query=False, show_progress=True)

            for (file_id, s, _), vec in zip(batch, embeddings, strict=True):
                all_data.append(
                    {
                        "file_id": file_id,
                        "symbol_name": s.name,
                        "symbol_type": s.symbol_type,
                        "language": s.language,
                        "start_line": s.start_line,
                        "end_line": s.end_line,
                        "text": s.text[:MAX_EMBED_CHARS],
                        "parent_name": s.parent_name,
                        "vector": vec,
                    }
                )

            done = min(batch_start + batch_size, total)
            logger.info(f"  Embedded {done}/{total} symbols ({done * 100 // total}%)")

        # Incremental write: only update changed files, preserve existing embeddings
        if table_name in self.db.table_names():
            table = self.db.open_table(table_name)
            # Delete old embeddings for changed files
            changed_file_ids = list({item[0] for item in all_items})
            id_list = ", ".join(str(fid) for fid in changed_file_ids)
            table.delete(f"file_id IN ({id_list})")
            table.add(all_data)
        else:
            self.db.create_table(table_name, all_data)

        logger.info(f"Batch embedding complete: {total} symbols stored")
        return total

    def search(
        self,
        query: str,
        limit: int = 20,
        filter_expr: str | None = None,
    ) -> list[dict]:
        """Search code symbols by semantic similarity."""
        if not self.is_available:
            return []

        try:
            table = self.db.open_table(self.TABLE_NAME)
        except Exception:
            return []

        query_embedding = self._embed_texts([query], is_query=True)[0]

        search_builder = table.search(query_embedding).limit(limit)
        if filter_expr:
            search_builder = search_builder.where(filter_expr)

        try:
            results = search_builder.to_list()
        except Exception as e:
            logger.warning(f"Code vector search failed: {e}")
            return []

        return [
            {
                "file_id": r["file_id"],
                "symbol_name": r["symbol_name"],
                "symbol_type": r["symbol_type"],
                "language": r["language"],
                "text": r["text"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "score": r.get("_distance", 0.0),
                "parent_name": r.get("parent_name", ""),
            }
            for r in results
        ]

    def compact(self) -> None:
        """Compact LanceDB table to defragment after incremental updates."""
        table_name = self.TABLE_NAME
        if table_name not in self.db.table_names():
            logger.info("No vector table to compact")
            return
        table = self.db.open_table(table_name)
        logger.info("Compacting vector index...")
        table.compact_files()
        logger.info("Vector index compaction complete")

    def remove_file(self, file_id: int) -> None:
        """Remove all symbols for a file from vector index."""
        try:
            table = self.db.open_table(self.TABLE_NAME)
            table.delete(f"file_id = {file_id}")
        except Exception:
            pass

    def get_stats(self) -> dict[str, int | float]:
        """Get vector index statistics."""
        import os

        db_path = settings.vector_db_path
        size_bytes = 0
        if db_path.exists():
            for f in db_path.rglob("*"):
                if f.is_file():
                    size_bytes += os.path.getsize(f)
        return {
            "index_size_mb": size_bytes / (1024 * 1024),
        }
