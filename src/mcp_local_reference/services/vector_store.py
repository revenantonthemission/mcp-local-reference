"""ChromaDB-backed vector store for semantic search over Zotero references."""

from __future__ import annotations

import json
import os
from typing import Any

import chromadb

from mcp_local_reference.config import Config


class VectorStore:
    """Manages a ChromaDB collection that stores embedded Zotero reference text.

    Documents are built from title + abstract + tags + author names so that
    semantic queries match on meaning rather than exact keywords.
    """

    COLLECTION_NAME = "zotero_references"
    METADATA_FILE = "index_metadata.json"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: chromadb.ClientAPI | None = None
        self._collection: chromadb.Collection | None = None

    # ------------------------------------------------------------------
    # Lazy initialisation
    # ------------------------------------------------------------------

    def _get_client(self) -> chromadb.ClientAPI:
        if self._client is None:
            self.config.chroma_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.config.chroma_dir))
        return self._client

    def _get_collection(self) -> chromadb.Collection:
        if self._collection is None:
            client = self._get_client()
            self._collection = client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_indexed(self) -> bool:
        """Return True if the vector index contains at least one document."""
        try:
            return self._get_collection().count() > 0
        except Exception:
            return False

    def needs_reindex(self) -> bool:
        """Return True if the Zotero DB has been modified since last indexing."""
        metadata_path = self.config.chroma_dir / self.METADATA_FILE
        if not metadata_path.exists():
            return True
        try:
            metadata = json.loads(metadata_path.read_text())
            current_mtime = os.path.getmtime(self.config.zotero_db_path)
            return current_mtime > metadata.get("zotero_db_mtime", 0)
        except (json.JSONDecodeError, OSError):
            return True

    def index_references(self, references: list[dict[str, Any]]) -> int:
        """(Re)build the vector index from a list of reference dicts.

        Returns the number of references successfully indexed.
        """
        collection = self._get_collection()

        # Clear existing data
        existing = collection.get()
        if existing["ids"]:
            collection.delete(ids=existing["ids"])

        if not references:
            return 0

        documents: list[str] = []
        metadatas: list[dict[str, str]] = []
        ids: list[str] = []

        for ref in references:
            doc = self._build_document(ref)
            if not doc.strip():
                continue

            documents.append(doc)
            metadatas.append(
                {
                    "item_key": str(ref.get("item_key", "")),
                    "title": str(ref.get("title", ""))[:500],
                    "item_type": str(ref.get("item_type", "")),
                    "date": str(ref.get("date", "")),
                }
            )
            ids.append(f"ref_{ref['item_key']}")

        if not documents:
            return 0

        # ChromaDB batch-size limit
        batch_size = 500
        for i in range(0, len(documents), batch_size):
            end = min(i + batch_size, len(documents))
            collection.add(
                documents=documents[i:end],
                metadatas=metadatas[i:end],
                ids=ids[i:end],
            )

        self._save_metadata(len(documents))
        return len(documents)

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Semantic search — returns a list of dicts with item_key, title, score."""
        collection = self._get_collection()
        count = collection.count()
        if count == 0:
            return []

        results = collection.query(
            query_texts=[query],
            n_results=min(limit, count),
        )

        items: list[dict[str, Any]] = []
        if results["metadatas"] and results["distances"]:
            for metadata, distance in zip(results["metadatas"][0], results["distances"][0]):
                items.append(
                    {
                        "item_key": metadata["item_key"],
                        "title": metadata["title"],
                        "item_type": metadata["item_type"],
                        "date": metadata["date"],
                        "relevance_score": round(1 - distance, 4),
                    }
                )
        return items

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_document(ref: dict[str, Any]) -> str:
        parts: list[str] = []
        if ref.get("title"):
            parts.append(ref["title"])
        if ref.get("abstract"):
            parts.append(ref["abstract"])
        if ref.get("tags"):
            parts.append(" ".join(ref["tags"]))
        if ref.get("creators"):
            names = [
                f"{c.get('firstName', '')} {c.get('lastName', '')}".strip() for c in ref["creators"]
            ]
            parts.append(" ".join(n for n in names if n))
        return "\n".join(parts)

    def _save_metadata(self, count: int) -> None:
        metadata_path = self.config.chroma_dir / self.METADATA_FILE
        zotero_db_mtime = os.path.getmtime(self.config.zotero_db_path)
        metadata_path.write_text(
            json.dumps({"indexed_count": count, "zotero_db_mtime": zotero_db_mtime})
        )
