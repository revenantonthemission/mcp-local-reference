"""Code index manager coordinating parsing, FTS5, and vector search."""

import asyncio
import logging
import threading
from pathlib import Path

from .code_embedder import CodeEmbedder
from .code_fts import CodeFTSIndex
from .config import settings
from .parser import TreeSitterParser, supported_extensions

logger = logging.getLogger(__name__)

# Default exclusion patterns
DEFAULT_EXCLUDE_PATTERNS: list[str] = [
    ".git",
    "vendor",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "target",
    "build",
    "dist",
    ".tox",
    "testdata",
]


class CodeIndexManager:
    """Facade coordinating source code indexing and search.

    Manages:
    - TreeSitterParser for symbol extraction
    - CodeFTSIndex for SQLite keyword search
    - CodeEmbedder for vector semantic search
    """

    def __init__(
        self,
        parser: TreeSitterParser | None = None,
        fts_index: CodeFTSIndex | None = None,
        embedder: CodeEmbedder | None = None,
    ):
        self.parser = parser or TreeSitterParser()
        self.fts_index = fts_index or CodeFTSIndex()
        self.embedder = embedder or CodeEmbedder()

    def index_repo(
        self,
        repo_path: Path,
        name: str | None = None,
        rebuild: bool = False,
        limit: int | None = None,
        skip_vectors: bool = False,
        background_vectors: bool = False,
    ) -> dict[str, int]:
        """Index a source code repository using 2-phase approach.

        Phase 1: Parse all files and build FTS index (fast, CPU-bound).
        Phase 2: Batch embed all symbols for vector search (slow, GPU/CPU-bound).

        Args:
            skip_vectors: If True, skip Phase 2 entirely (FTS-only, ~10x faster).
            background_vectors: If True, run Phase 2 in a background thread
                (returns immediately after Phase 1).

        Returns stats dict: {total, indexed, skipped, failed, symbols}
        """
        repo_path = repo_path.expanduser().resolve()
        if not repo_path.is_dir():
            raise FileNotFoundError(f"Repository not found: {repo_path}")

        repo_name = name or repo_path.name
        repo_id = self.fts_index.upsert_repo(repo_name, repo_path)

        # Collect eligible files
        max_size = settings.max_file_size_kb * 1024
        extensions = supported_extensions()
        files = self._collect_files(repo_path, extensions, max_size, limit)

        total = len(files)
        logger.info(f"Indexing repo '{repo_name}': {total} eligible files")

        stats = {"total": total, "indexed": 0, "skipped": 0, "failed": 0, "symbols": 0}

        # Phase 1a: Parse files (mtime-first skip, then hash check)
        parsed_files: list[tuple[str, str, int, str, float | None, list]] = []
        existing_hashes = self.fts_index.get_all_file_hashes(repo_id)
        mtime_updates: list[tuple[str, float]] = []  # files with stale mtime but same hash

        for i, file_path in enumerate(files, 1):
            rel_path = str(file_path.relative_to(repo_path))
            try:
                current_mtime = file_path.stat().st_mtime

                # Fast path: check mtime first
                stored = existing_hashes.get(rel_path)
                if not rebuild and stored is not None:
                    stored_hash, stored_mtime = stored
                    if stored_mtime is not None and stored_mtime == current_mtime:
                        # mtime matches — skip without reading file
                        stats["skipped"] += 1
                        continue

                    # mtime differs — check hash
                    file_hash = CodeFTSIndex.compute_file_hash(file_path)
                    if stored_hash == file_hash:
                        # Content unchanged, just mtime drift — update mtime, skip
                        mtime_updates.append((rel_path, current_mtime))
                        stats["skipped"] += 1
                        continue
                else:
                    file_hash = CodeFTSIndex.compute_file_hash(file_path)

                # File is new or changed — parse it
                result = self._parse_file_for_index(file_path, rel_path, file_hash)
                if result == "failed":
                    stats["failed"] += 1
                else:
                    # Add current_mtime to the tuple for batch write
                    rel_path_r, language, size_bytes, fh, symbols = result
                    parsed_files.append(
                        (rel_path_r, language, size_bytes, fh, current_mtime, symbols)
                    )
                    stats["indexed"] += 1
                    stats["symbols"] += len(symbols)
            except Exception as e:
                logger.error(f"[{i}/{total}] Failed {rel_path}: {e}")
                stats["failed"] += 1

            if i % 500 == 0 or i == total:
                logger.info(
                    f"  Phase 1a parse: {i}/{total} files "
                    f"({stats['indexed']} parsed, {stats['skipped']} skipped)"
                )

        # Batch-update mtimes for files that had stale mtime but matching hash
        if mtime_updates:
            self.fts_index.update_mtimes_batch(repo_id, mtime_updates)

        # Phase 1b: Batch write to FTS (single transaction)
        file_symbols: list[tuple[int, list]] = []
        if parsed_files:
            logger.info(f"Phase 1b: batch writing {len(parsed_files)} files to FTS index...")
            try:
                batch_results = self.fts_index.add_files_batch(repo_id, parsed_files)
                if not skip_vectors:
                    file_symbols = batch_results
            except Exception as e:
                logger.error(f"Phase 1b batch write failed: {e}")
                stats["failed"] += len(parsed_files)
                stats["indexed"] = 0

        logger.info(
            f"Phase 1 done: {stats['indexed']} indexed, "
            f"{stats['skipped']} skipped, {stats['failed']} failed, "
            f"{stats['symbols']} symbols"
        )

        # Phase 2: Batch embed all symbols (optional)
        if skip_vectors or not file_symbols:
            if skip_vectors:
                logger.info("Phase 2 skipped (--skip-vectors)")
            return stats

        if background_vectors:
            sym_count = sum(len(syms) for _, syms in file_symbols)
            logger.info(f"Phase 2 starting in background thread ({sym_count} symbols)...")

            def _bg_embed():
                try:
                    embedded = self.embedder.add_symbols_batch(file_symbols)
                    logger.info(f"Phase 2 background done: {embedded} symbols embedded")
                except Exception as e:
                    logger.warning(f"Phase 2 background failed: {e}")

            thread = threading.Thread(target=_bg_embed, daemon=True)
            thread.start()
            self._embed_thread = thread  # keep reference for join if needed
        else:
            try:
                embedded = self.embedder.add_symbols_batch(file_symbols)
                logger.info(f"Phase 2 done: {embedded} symbols embedded")
            except Exception as e:
                logger.warning(f"Phase 2 (vector embedding) failed: {e}")

        return stats

    def _parse_file_for_index(
        self,
        file_path: Path,
        rel_path: str,
        file_hash: str,
    ) -> tuple[str, str, int, str, list] | str:
        """Parse a file for batch indexing (no DB writes).

        Returns (rel_path, language, size_bytes, file_hash, symbols) or "failed".
        """
        if self.parser.can_parse(file_path) or self.parser.is_doc_file(file_path):
            symbols = self.parser.parse_file(file_path)
        else:
            return "failed"

        if not symbols:
            return "failed"

        language = symbols[0].language
        size_bytes = file_path.stat().st_size
        return (rel_path, language, size_bytes, file_hash, symbols)

    def _index_file_fts(
        self,
        repo_id: int,
        file_path: Path,
        rel_path: str,
        rebuild: bool,
    ) -> tuple[int, list] | str:
        """Parse and FTS-index a single file (Phase 1).

        Returns (file_id, symbols) on success, or "skipped"/"failed".
        """
        # Change detection
        file_hash = CodeFTSIndex.compute_file_hash(file_path)
        if not rebuild and not self.fts_index.needs_indexing(repo_id, rel_path, file_hash):
            return "skipped"

        # Parse symbols (parse_file handles both code and doc files)
        if self.parser.can_parse(file_path) or self.parser.is_doc_file(file_path):
            symbols = self.parser.parse_file(file_path)
        else:
            return "failed"

        if not symbols:
            return "failed"

        # Upsert file record
        file_id = self.fts_index.upsert_file(
            repo_id,
            rel_path,
            language=symbols[0].language,
            size_bytes=file_path.stat().st_size,
            file_hash=file_hash,
        )

        # Set file_id on symbols
        for s in symbols:
            s.file_id = file_id

        # FTS index
        try:
            self.fts_index.add_symbols(file_id, symbols)
        except Exception as e:
            logger.error(f"FTS indexing failed for {rel_path}: {e}")
            return "failed"

        return (file_id, symbols)

    def _index_file(
        self,
        repo_id: int,
        file_path: Path,
        rel_path: str,
        rebuild: bool,
    ) -> int | str:
        """Index a single file (FTS + vector). Used by reindex_file().

        Returns symbol count or "skipped"/"failed".
        """
        result = self._index_file_fts(repo_id, file_path, rel_path, rebuild)
        if isinstance(result, str):
            return result

        file_id, symbols = result

        # Vector index (optional, graceful degradation)
        try:
            self.embedder.add_symbols(file_id, symbols)
        except Exception as e:
            logger.warning(f"Vector indexing failed for {rel_path}: {e}")

        return len(symbols)

    def _collect_files(
        self,
        repo_path: Path,
        extensions: set[str],
        max_size: int,
        limit: int | None,
    ) -> list[Path]:
        """Walk repo and collect eligible source/doc files."""
        files: list[Path] = []
        for path in sorted(repo_path.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in extensions:
                continue
            if self._should_exclude(path, repo_path):
                continue
            try:
                size = path.stat().st_size
                if size > max_size or size == 0:
                    continue
            except OSError:
                continue
            files.append(path)
            if limit and len(files) >= limit:
                break
        return files

    @staticmethod
    def _should_exclude(path: Path, repo_root: Path) -> bool:
        """Check if file should be excluded based on path patterns."""
        rel = path.relative_to(repo_root)
        parts = rel.parts
        for part in parts:
            if part.startswith("."):
                return True
            if part in DEFAULT_EXCLUDE_PATTERNS:
                return True
        return False

    # --- Single file operations ---

    def reindex_file(self, repo_name: str, repo_path: Path, file_path: Path) -> str:
        """Reindex a single file. Returns "indexed", "skipped", or "failed"."""
        repo_path = repo_path.expanduser().resolve()
        repo_id = self.fts_index.upsert_repo(repo_name, repo_path)
        rel_path = str(file_path.relative_to(repo_path))

        result = self._index_file(repo_id, file_path, rel_path, rebuild=False)
        if result == "skipped":
            return "skipped"
        elif result == "failed":
            return "failed"
        return "indexed"

    def remove_file_by_path(self, repo_name: str, rel_path: str) -> bool:
        """Remove a file from the index by repo name and relative path."""
        repo_ids = self.fts_index.get_repo_ids_by_names([repo_name])
        if not repo_ids:
            return False

        with self.fts_index._connect() as conn:
            row = conn.execute(
                "SELECT file_id FROM code_files WHERE repo_id = ? AND rel_path = ?",
                (repo_ids[0], rel_path),
            ).fetchone()
            if not row:
                return False

            file_id = row["file_id"]
            conn.execute("DELETE FROM code_symbols WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM code_files WHERE file_id = ?", (file_id,))

        try:
            self.embedder.remove_file(file_id)
        except Exception as e:
            logger.warning(f"Failed to remove vector data for file_id {file_id}: {e}")

        logger.info(f"Removed {repo_name}:{rel_path} from index")
        return True

    # --- Search ---

    def keyword_search(
        self,
        query: str,
        limit: int = 20,
        repos: list[str] | None = None,
        languages: list[str] | None = None,
    ) -> list[dict]:
        """Search code using FTS5 keyword search."""
        if not query.strip():
            return []

        repo_ids = self.fts_index.get_repo_ids_by_names(repos) if repos else None
        lang_set = set(languages) if languages else None

        # Check if repo filter resulted in empty set
        if repo_ids is not None and not repo_ids:
            return []

        results = self.fts_index.search(query, limit, repo_ids, lang_set)
        return self._enrich_results(results, source="keyword")

    def semantic_search(
        self,
        query: str,
        limit: int = 20,
        repos: list[str] | None = None,
        languages: list[str] | None = None,
    ) -> list[dict]:
        """Search code using vector semantic search."""
        if not query.strip() or not self.embedder.is_available:
            return []

        # Build LanceDB filter expression
        filter_parts: list[str] = []
        if repos:
            repo_ids = self.fts_index.get_repo_ids_by_names(repos)
            if not repo_ids:
                return []
            file_ids_for_repos = self.fts_index.get_file_ids_for_repos(repo_ids)
            if not file_ids_for_repos:
                return []
            id_list = ", ".join(str(fid) for fid in file_ids_for_repos)
            filter_parts.append(f"file_id IN ({id_list})")

        if languages:
            from .parser import EXTENSION_TO_LANGUAGE

            # Whitelist validation prevents injection — LanceDB doesn't support
            # parameterized queries, so we only allow known language names.
            valid_languages = set(EXTENSION_TO_LANGUAGE.values()) | {"text"}
            sanitized = [lang for lang in languages if lang in valid_languages]
            if sanitized:
                lang_list = ", ".join(f"'{lang}'" for lang in sanitized)
                filter_parts.append(f"language IN ({lang_list})")

        filter_expr = " AND ".join(filter_parts) if filter_parts else None

        results = self.embedder.search(query, limit, filter_expr)

        # Enrich with file metadata
        file_ids = list({r["file_id"] for r in results})
        files_meta = self.fts_index.get_files_batch(file_ids)

        enriched = []
        for r in results:
            file_meta = files_meta.get(r["file_id"], {})
            enriched.append(
                {
                    "file_id": r["file_id"],
                    "symbol_name": r["symbol_name"],
                    "symbol_type": r["symbol_type"],
                    "text": r["text"],
                    "score": r["score"],
                    "start_line": r["start_line"],
                    "end_line": r["end_line"],
                    "language": r["language"],
                    "parent_name": r.get("parent_name", ""),
                    "repo_name": file_meta.get("repo_name", ""),
                    "rel_path": file_meta.get("rel_path", ""),
                    "source": "semantic",
                }
            )
        return enriched

    async def hybrid_search(
        self,
        query: str,
        limit: int = 20,
        repos: list[str] | None = None,
        languages: list[str] | None = None,
    ) -> list[dict]:
        """Hybrid search combining keyword + semantic with RRF."""
        keyword_results, semantic_results = await asyncio.gather(
            asyncio.to_thread(self.keyword_search, query, limit * 3, repos, languages),
            asyncio.to_thread(self.semantic_search, query, limit * 3, repos, languages),
        )

        # Reciprocal Rank Fusion (k=60)
        k = 60
        scores: dict[tuple, float] = {}
        result_map: dict[tuple, dict] = {}

        for rank, r in enumerate(keyword_results):
            key = (r["file_id"], r["symbol_name"], r["start_line"])
            scores[key] = scores.get(key, 0) + 1 / (k + rank + 1)
            result_map[key] = r

        for rank, r in enumerate(semantic_results):
            key = (r["file_id"], r["symbol_name"], r["start_line"])
            scores[key] = scores.get(key, 0) + 1 / (k + rank + 1)
            if key not in result_map:
                result_map[key] = r

        # Sort by RRF score descending
        sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)

        results = []
        for key in sorted_keys[:limit]:
            r = result_map[key]
            r["score"] = scores[key]
            r["source"] = "hybrid"
            results.append(r)

        return results

    def _enrich_results(self, fts_results: list, source: str) -> list[dict]:
        """Convert FTS results to enriched dicts with file metadata."""
        if not fts_results:
            return []

        file_ids = list({r.file_id for r in fts_results})
        files_meta = self.fts_index.get_files_batch(file_ids)

        enriched = []
        for r in fts_results:
            file_meta = files_meta.get(r.file_id, {})
            enriched.append(
                {
                    "file_id": r.file_id,
                    "symbol_name": r.symbol_name,
                    "symbol_type": r.symbol_type,
                    "text": r.text,
                    "score": r.score,
                    "highlights": r.highlights,
                    "start_line": r.start_line,
                    "end_line": r.end_line,
                    "language": r.language,
                    "parent_name": r.parent_name,
                    "repo_name": file_meta.get("repo_name", ""),
                    "rel_path": file_meta.get("rel_path", ""),
                    "source": source,
                }
            )
        return enriched

    # --- Stats ---

    def get_stats(self) -> dict:
        """Get combined index statistics."""
        fts_stats = self.fts_index.get_stats()
        vector_stats = self.embedder.get_stats()
        return {
            **fts_stats,
            "vector_index_size_mb": vector_stats.get("index_size_mb", 0.0),
            "total_index_size_mb": (
                fts_stats.get("index_size_mb", 0.0) + vector_stats.get("index_size_mb", 0.0)
            ),
        }

    def list_repos(self) -> list[dict]:
        """List all indexed repositories."""
        return self.fts_index.list_repos()

    def remove_repo(self, name: str) -> None:
        """Remove a repository from the index."""
        # Clean vector data for all files in this repo before removing FTS records
        repo_ids = self.fts_index.get_repo_ids_by_names([name])
        if repo_ids:
            file_ids = self.fts_index.get_file_ids_for_repo(repo_ids[0])
            for fid in file_ids:
                try:
                    self.embedder.remove_file(fid)
                except Exception as e:
                    logger.warning(f"Failed to remove vector data for file_id {fid}: {e}")
        self.fts_index.remove_repo(name)
