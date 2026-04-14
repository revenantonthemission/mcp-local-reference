"""SQLite FTS5 index for source code symbols."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class CodeSearchResult:
    """A single code search result from FTS5."""

    file_id: int
    symbol_name: str
    symbol_type: str
    text: str
    score: float
    highlights: str
    start_line: int
    end_line: int
    language: str
    parent_name: str = ""


class CodeFTSIndex:
    """SQLite FTS5 full-text index for source code symbols."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or settings.index_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        """Close the persistent connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _init_db(self) -> None:
        with self._connect() as conn:
            result = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if result and result["journal_mode"] != "wal":
                logger.warning(f"Failed to enable WAL mode, got: {result['journal_mode']}")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS repositories (
                    repo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    path TEXT NOT NULL,
                    indexed_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS code_files (
                    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_id INTEGER NOT NULL REFERENCES repositories(repo_id),
                    rel_path TEXT NOT NULL,
                    language TEXT,
                    size_bytes INTEGER DEFAULT 0,
                    file_hash TEXT,
                    indexed_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(repo_id, rel_path)
                );

                CREATE TABLE IF NOT EXISTS code_symbols (
                    symbol_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL REFERENCES code_files(file_id),
                    symbol_name TEXT NOT NULL,
                    symbol_type TEXT NOT NULL,
                    signature TEXT,
                    text TEXT NOT NULL,
                    start_line INTEGER,
                    end_line INTEGER,
                    language TEXT
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS code_symbols_fts USING fts5(
                    symbol_name,
                    text,
                    content='code_symbols',
                    content_rowid='symbol_id',
                    tokenize='porter unicode61'
                );

                -- Triggers for FTS sync
                -- Migration: add parent_name column if missing
                -- (safe to run repeatedly — ALTER TABLE IF NOT EXISTS not supported,
                --  so we catch the error if column already exists)
            """)

            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE code_symbols ADD COLUMN parent_name TEXT DEFAULT ''")

            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE code_files ADD COLUMN file_mtime REAL")

            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS code_symbols_ai AFTER INSERT ON code_symbols
                BEGIN
                    INSERT INTO code_symbols_fts(rowid, symbol_name, text)
                    VALUES (new.symbol_id, new.symbol_name, new.text);
                END;

                CREATE TRIGGER IF NOT EXISTS code_symbols_ad AFTER DELETE ON code_symbols
                BEGIN
                    INSERT INTO code_symbols_fts(code_symbols_fts, rowid, symbol_name, text)
                    VALUES ('delete', old.symbol_id, old.symbol_name, old.text);
                END;

                CREATE TRIGGER IF NOT EXISTS code_symbols_au AFTER UPDATE ON code_symbols
                BEGIN
                    INSERT INTO code_symbols_fts(code_symbols_fts, rowid, symbol_name, text)
                    VALUES ('delete', old.symbol_id, old.symbol_name, old.text);
                    INSERT INTO code_symbols_fts(rowid, symbol_name, text)
                    VALUES (new.symbol_id, new.symbol_name, new.text);
                END;
            """)

    def upsert_repo(self, name: str, path: Path) -> int:
        """Insert or update a repository, return repo_id."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO repositories (name, path) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET path=excluded.path, "
                "indexed_at=datetime('now')",
                (name, str(path)),
            )
            row = conn.execute(
                "SELECT repo_id FROM repositories WHERE name = ?", (name,)
            ).fetchone()
            return row["repo_id"]

    def upsert_file(
        self,
        repo_id: int,
        rel_path: str,
        language: str,
        size_bytes: int,
        file_hash: str,
        file_mtime: float | None = None,
    ) -> int:
        """Insert or update a file record, return file_id."""
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT file_id FROM code_files WHERE repo_id = ? AND rel_path = ?",
                (repo_id, rel_path),
            ).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM code_symbols WHERE file_id = ?",
                    (existing["file_id"],),
                )

            conn.execute(
                "INSERT INTO code_files (repo_id, rel_path, language, size_bytes, file_hash, file_mtime) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repo_id, rel_path) DO UPDATE SET "
                "language=excluded.language, size_bytes=excluded.size_bytes, "
                "file_hash=excluded.file_hash, file_mtime=excluded.file_mtime, "
                "indexed_at=datetime('now')",
                (repo_id, rel_path, language, size_bytes, file_hash, file_mtime),
            )
            row = conn.execute(
                "SELECT file_id FROM code_files WHERE repo_id = ? AND rel_path = ?",
                (repo_id, rel_path),
            ).fetchone()
            return row["file_id"]

    def get_all_file_hashes(self, repo_id: int) -> dict[str, tuple[str, float | None]]:
        """Get all (rel_path -> (file_hash, file_mtime)) for a repo in one query."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT rel_path, file_hash, file_mtime FROM code_files WHERE repo_id = ?",
                (repo_id,),
            ).fetchall()
            return {
                row["rel_path"]: (row["file_hash"], row["file_mtime"])
                for row in rows
            }

    def update_mtimes_batch(self, repo_id: int, updates: list[tuple[str, float]]) -> None:
        """Batch-update file_mtime for files whose hash matched but mtime was stale."""
        if not updates:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE code_files SET file_mtime = ? WHERE repo_id = ? AND rel_path = ?",
                [(mtime, repo_id, rel_path) for rel_path, mtime in updates],
            )

    def add_symbols(self, file_id: int, symbols: list) -> None:
        """Add symbols for a file (triggers update FTS automatically)."""
        if not symbols:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO code_symbols "
                "(file_id, symbol_name, symbol_type, signature, text, "
                "start_line, end_line, language, parent_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        file_id,
                        s.name,
                        s.symbol_type,
                        s.signature,
                        s.text,
                        s.start_line,
                        s.end_line,
                        s.language,
                        s.parent_name,
                    )
                    for s in symbols
                ],
            )

    def add_files_batch(
        self,
        repo_id: int,
        files_data: list[tuple[str, str, int, str, float | None, list]],
    ) -> list[tuple[int, list]]:
        """Batch insert files and symbols in a single transaction.

        Disables FTS triggers during bulk insert and rebuilds FTS index
        once at the end, which is much faster than per-row trigger updates.

        Args:
            repo_id: Repository ID.
            files_data: List of (rel_path, language, size_bytes, file_hash, file_mtime, symbols).

        Returns:
            List of (file_id, symbols) tuples for downstream use (e.g. embedding).
        """
        if not files_data:
            return []

        results: list[tuple[int, list]] = []
        with self._connect() as conn:
            # Suspend FTS triggers — rebuild index once at the end
            conn.execute("DROP TRIGGER IF EXISTS code_symbols_ai")
            conn.execute("DROP TRIGGER IF EXISTS code_symbols_ad")
            conn.execute("DROP TRIGGER IF EXISTS code_symbols_au")

            # Upsert files and collect file_ids
            file_id_map: dict[str, int] = {}
            for rel_path, language, size_bytes, file_hash, file_mtime, symbols in files_data:
                existing = conn.execute(
                    "SELECT file_id FROM code_files WHERE repo_id = ? AND rel_path = ?",
                    (repo_id, rel_path),
                ).fetchone()
                if existing:
                    conn.execute(
                        "DELETE FROM code_symbols WHERE file_id = ?",
                        (existing["file_id"],),
                    )

                conn.execute(
                    "INSERT INTO code_files (repo_id, rel_path, language, size_bytes, file_hash, file_mtime) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(repo_id, rel_path) DO UPDATE SET "
                    "language=excluded.language, size_bytes=excluded.size_bytes, "
                    "file_hash=excluded.file_hash, file_mtime=excluded.file_mtime, "
                    "indexed_at=datetime('now')",
                    (repo_id, rel_path, language, size_bytes, file_hash, file_mtime),
                )
                row = conn.execute(
                    "SELECT file_id FROM code_files WHERE repo_id = ? AND rel_path = ?",
                    (repo_id, rel_path),
                ).fetchone()
                file_id_map[rel_path] = row["file_id"]

            # Bulk insert ALL symbols in one executemany call
            all_symbol_rows: list[tuple] = []
            for rel_path, language, size_bytes, file_hash, file_mtime, symbols in files_data:
                file_id = file_id_map[rel_path]
                for s in symbols:
                    s.file_id = file_id
                    all_symbol_rows.append((
                        file_id, s.name, s.symbol_type, s.signature,
                        s.text, s.start_line, s.end_line, s.language, s.parent_name,
                    ))
                results.append((file_id, symbols))

            if all_symbol_rows:
                # Track first new symbol_id for selective FTS insert
                row = conn.execute(
                    "SELECT COALESCE(MAX(symbol_id), 0) FROM code_symbols"
                ).fetchone()
                first_new_id = row[0] + 1

                conn.executemany(
                    "INSERT INTO code_symbols "
                    "(file_id, symbol_name, symbol_type, signature, text, "
                    "start_line, end_line, language, parent_name) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    all_symbol_rows,
                )

                # Insert FTS entries only for newly added symbols
                # (much faster than full 'rebuild' when DB already has many symbols)
                conn.execute(
                    "INSERT INTO code_symbols_fts(rowid, symbol_name, text) "
                    "SELECT symbol_id, symbol_name, text FROM code_symbols "
                    "WHERE symbol_id >= ?",
                    (first_new_id,),
                )

            # Restore triggers
            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS code_symbols_ai AFTER INSERT ON code_symbols
                BEGIN
                    INSERT INTO code_symbols_fts(rowid, symbol_name, text)
                    VALUES (new.symbol_id, new.symbol_name, new.text);
                END;

                CREATE TRIGGER IF NOT EXISTS code_symbols_ad AFTER DELETE ON code_symbols
                BEGIN
                    INSERT INTO code_symbols_fts(code_symbols_fts, rowid, symbol_name, text)
                    VALUES ('delete', old.symbol_id, old.symbol_name, old.text);
                END;

                CREATE TRIGGER IF NOT EXISTS code_symbols_au AFTER UPDATE ON code_symbols
                BEGIN
                    INSERT INTO code_symbols_fts(code_symbols_fts, rowid, symbol_name, text)
                    VALUES ('delete', old.symbol_id, old.symbol_name, old.text);
                    INSERT INTO code_symbols_fts(rowid, symbol_name, text)
                    VALUES (new.symbol_id, new.symbol_name, new.text);
                END;
            """)

        return results

    def needs_indexing(self, repo_id: int, rel_path: str, file_hash: str) -> bool:
        """Check if file needs (re-)indexing based on content hash."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT file_hash FROM code_files WHERE repo_id = ? AND rel_path = ?",
                (repo_id, rel_path),
            ).fetchone()
            if not row:
                return True
            return row["file_hash"] != file_hash

    def search(
        self,
        query: str,
        limit: int = 20,
        repo_ids: list[int] | None = None,
        languages: set[str] | None = None,
    ) -> list[CodeSearchResult]:
        """Search code symbols using FTS5."""
        with self._connect() as conn:
            # Build WHERE clause for filters
            where_parts = []
            params: list = []

            if repo_ids:
                placeholders = ",".join("?" * len(repo_ids))
                where_parts.append(
                    f"cs.file_id IN (SELECT file_id FROM code_files "
                    f"WHERE repo_id IN ({placeholders}))"
                )
                params.extend(repo_ids)

            if languages:
                placeholders = ",".join("?" * len(languages))
                where_parts.append(f"cs.language IN ({placeholders})")
                params.extend(languages)

            where_clause = ""
            if where_parts:
                where_clause = "AND " + " AND ".join(where_parts)

            sql = f"""
                SELECT
                    cs.file_id,
                    cs.symbol_name,
                    cs.symbol_type,
                    cs.text,
                    cs.start_line,
                    cs.end_line,
                    cs.language,
                    cs.parent_name,
                    fts.rank AS score,
                    highlight(code_symbols_fts, 1, '<mark>', '</mark>') AS highlights
                FROM code_symbols_fts fts
                JOIN code_symbols cs ON cs.symbol_id = fts.rowid
                WHERE code_symbols_fts MATCH ?
                {where_clause}
                ORDER BY fts.rank
                LIMIT ?
            """
            params = [query] + params + [limit]

            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as e:
                logger.warning(f"FTS search failed: {e}")
                return []

            return [
                CodeSearchResult(
                    file_id=row["file_id"],
                    symbol_name=row["symbol_name"],
                    symbol_type=row["symbol_type"],
                    text=row["text"],
                    score=abs(row["score"]),
                    highlights=row["highlights"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    language=row["language"],
                    parent_name=row["parent_name"] or "",
                )
                for row in rows
            ]

    def get_repo_ids_by_names(self, names: list[str]) -> list[int]:
        """Resolve repo names to repo_ids."""
        if not names:
            return []
        with self._connect() as conn:
            placeholders = ",".join("?" * len(names))
            rows = conn.execute(
                f"SELECT repo_id FROM repositories WHERE name IN ({placeholders})",
                names,
            ).fetchall()
            return [row["repo_id"] for row in rows]

    def get_files_batch(self, file_ids: list[int]) -> dict[int, dict]:
        """Get file metadata for a batch of file_ids."""
        if not file_ids:
            return {}
        with self._connect() as conn:
            placeholders = ",".join("?" * len(file_ids))
            rows = conn.execute(
                f"SELECT cf.file_id, cf.rel_path, cf.language, r.name AS repo_name "
                f"FROM code_files cf JOIN repositories r ON r.repo_id = cf.repo_id "
                f"WHERE cf.file_id IN ({placeholders})",
                file_ids,
            ).fetchall()
            return {
                row["file_id"]: {
                    "rel_path": row["rel_path"],
                    "language": row["language"],
                    "repo_name": row["repo_name"],
                }
                for row in rows
            }

    def list_repos(self) -> list[dict]:
        """List all indexed repositories."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.repo_id, r.name, r.path, r.indexed_at, "
                "COUNT(DISTINCT cf.file_id) as file_count, "
                "COUNT(cs.symbol_id) as symbol_count "
                "FROM repositories r "
                "LEFT JOIN code_files cf ON cf.repo_id = r.repo_id "
                "LEFT JOIN code_symbols cs ON cs.file_id = cf.file_id "
                "GROUP BY r.repo_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_file_ids_for_repo(self, repo_id: int) -> list[int]:
        """Get all file_ids belonging to a repository."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT file_id FROM code_files WHERE repo_id = ?", (repo_id,)
            ).fetchall()
            return [row["file_id"] for row in rows]

    def get_file_ids_for_repos(self, repo_ids: list[int]) -> list[int]:
        """Get all file_ids belonging to multiple repositories."""
        if not repo_ids:
            return []
        with self._connect() as conn:
            placeholders = ",".join("?" * len(repo_ids))
            rows = conn.execute(
                f"SELECT file_id FROM code_files WHERE repo_id IN ({placeholders})",
                repo_ids,
            ).fetchall()
            return [row["file_id"] for row in rows]

    def get_symbols_by_file(self, repo_name: str, rel_path: str) -> list[dict]:
        """Get all symbols in a file (overview mode)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT cs.symbol_name, cs.symbol_type, cs.signature, "
                "cs.start_line, cs.end_line, cs.language, cs.parent_name "
                "FROM code_symbols cs "
                "JOIN code_files cf ON cf.file_id = cs.file_id "
                "JOIN repositories r ON r.repo_id = cf.repo_id "
                "WHERE r.name = ? AND cf.rel_path = ? "
                "ORDER BY cs.start_line",
                (repo_name, rel_path),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_symbol_by_name(
        self,
        repo_name: str,
        rel_path: str,
        symbol_name: str,
        parent_name: str | None = None,
    ) -> list[dict]:
        """Get symbols by name (full source). Returns all matches.

        If parent_name is specified, filters to symbols within that parent.
        """
        with self._connect() as conn:
            sql = (
                "SELECT cs.symbol_name, cs.symbol_type, cs.signature, cs.text, "
                "cs.start_line, cs.end_line, cs.language, cs.parent_name "
                "FROM code_symbols cs "
                "JOIN code_files cf ON cf.file_id = cs.file_id "
                "JOIN repositories r ON r.repo_id = cf.repo_id "
                "WHERE r.name = ? AND cf.rel_path = ? AND cs.symbol_name = ?"
            )
            params: list = [repo_name, rel_path, symbol_name]
            if parent_name is not None:
                sql += " AND cs.parent_name = ?"
                params.append(parent_name)
            sql += " ORDER BY cs.start_line"
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def remove_repo(self, name: str) -> None:
        """Remove a repository and all its data from the index."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT repo_id FROM repositories WHERE name = ?", (name,)
            ).fetchone()
            if not row:
                return
            repo_id = row["repo_id"]
            conn.execute(
                "DELETE FROM code_symbols WHERE file_id IN "
                "(SELECT file_id FROM code_files WHERE repo_id = ?)",
                (repo_id,),
            )
            conn.execute("DELETE FROM code_files WHERE repo_id = ?", (repo_id,))
            conn.execute("DELETE FROM repositories WHERE repo_id = ?", (repo_id,))

    def get_stats(self) -> dict:
        """Get index statistics."""
        import os

        with self._connect() as conn:
            repo_count = conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
            file_count = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
            symbol_count = conn.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0]

        size_bytes = os.path.getsize(self.db_path) if self.db_path.exists() else 0
        return {
            "repo_count": repo_count,
            "file_count": file_count,
            "symbol_count": symbol_count,
            "index_size_mb": size_bytes / (1024 * 1024),
        }

    @staticmethod
    def compute_file_hash(file_path: Path) -> str:
        """Compute SHA256 hash of file content."""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
