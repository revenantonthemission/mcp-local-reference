"""Read-only client for Zotero's local SQLite database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from mcp_local_reference.config import Config


@dataclass
class Reference:
    """A Zotero reference item."""

    item_key: str
    item_type: str
    title: str = ""
    creators: list[dict[str, str]] = field(default_factory=list)
    date: str = ""
    abstract: str = ""
    doi: str = ""
    url: str = ""
    publication: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    place: str = ""
    edition: str = ""
    isbn: str = ""
    issn: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "item_key": self.item_key,
            "item_type": self.item_type,
            "title": self.title,
            "creators": self.creators,
            "date": self.date,
            "abstract": self.abstract,
            "doi": self.doi,
            "url": self.url,
            "publication": self.publication,
            "volume": self.volume,
            "issue": self.issue,
            "pages": self.pages,
            "publisher": self.publisher,
            "place": self.place,
            "edition": self.edition,
            "isbn": self.isbn,
            "issn": self.issn,
            "tags": self.tags,
        }


@dataclass
class Collection:
    """A Zotero collection (folder)."""

    key: str
    name: str
    parent_key: str | None = None
    children: list[Collection] = field(default_factory=list)


class ZoteroClient:
    """Read-only client for Zotero's local SQLite database.

    Opens the database in read-only mode so it is safe to use while Zotero
    is running.  A fresh connection is created for each public method call
    to avoid stale-handle issues.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        db_path = self.config.zotero_db_path
        if not db_path.exists():
            raise FileNotFoundError(
                f"Zotero database not found at {db_path}. "
                "Set ZOTERO_DATA_DIR to your Zotero data directory."
            )
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[Reference]:
        """Keyword search across titles, abstracts, and other metadata fields."""
        conn = self._connect()
        try:
            search_term = f"%{query}%"
            cursor = conn.execute(
                """
                SELECT DISTINCT i.itemID, i.key AS item_key, it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                JOIN itemData id ON i.itemID = id.itemID
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                WHERE idv.value LIKE ?
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND it.typeName NOT IN ('attachment', 'note', 'annotation')
                LIMIT ?
                """,
                (search_term, limit),
            )
            return [
                self._build_reference(conn, row["itemID"], row["item_key"], row["typeName"])
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def get_reference(self, item_key: str) -> Reference | None:
        """Get a single reference by its Zotero item key."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT i.itemID, i.key AS item_key, it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                WHERE i.key = ?
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                """,
                (item_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._build_reference(conn, row["itemID"], row["item_key"], row["typeName"])
        finally:
            conn.close()

    def list_collections(self) -> list[Collection]:
        """Return all Zotero collections as a tree."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT c.collectionID, c.collectionName, c.key,
                       pc.key AS parent_key
                FROM collections c
                LEFT JOIN collections pc ON c.parentCollectionID = pc.collectionID
                ORDER BY c.collectionName
                """
            )
            by_key: dict[str, Collection] = {}
            for row in cursor.fetchall():
                col = Collection(
                    key=row["key"],
                    name=row["collectionName"],
                    parent_key=row["parent_key"],
                )
                by_key[col.key] = col

            roots: list[Collection] = []
            for col in by_key.values():
                if col.parent_key and col.parent_key in by_key:
                    by_key[col.parent_key].children.append(col)
                else:
                    roots.append(col)
            return roots
        finally:
            conn.close()

    def get_collection_items(self, collection_key: str, limit: int = 50) -> list[Reference]:
        """Get items belonging to a specific collection."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT i.itemID, i.key AS item_key, it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                JOIN collectionItems ci ON i.itemID = ci.itemID
                JOIN collections c ON ci.collectionID = c.collectionID
                WHERE c.key = ?
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND it.typeName NOT IN ('attachment', 'note', 'annotation')
                ORDER BY ci.orderIndex
                LIMIT ?
                """,
                (collection_key, limit),
            )
            return [
                self._build_reference(conn, row["itemID"], row["item_key"], row["typeName"])
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def get_item_collections(self, item_key: str) -> list[str]:
        """Return the collection keys an item currently belongs to.

        Excludes items in the trash (``deletedItems``) for consistency with
        ``search`` / ``get_collection_items`` / ``get_reference``.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT c.key
                FROM collections c
                JOIN collectionItems ci ON c.collectionID = ci.collectionID
                JOIN items i ON ci.itemID = i.itemID
                WHERE i.key = ?
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                ORDER BY c.key
                """,
                (item_key,),
            )
            return [row["key"] for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_pdf_path(self, item_key: str) -> Path | None:
        """Resolve the filesystem path to a reference's PDF attachment."""
        conn = self._connect()
        try:
            cursor = conn.execute("SELECT itemID FROM items WHERE key = ?", (item_key,))
            row = cursor.fetchone()
            if row is None:
                return None

            item_id: int = row["itemID"]
            cursor = conn.execute(
                """
                SELECT ia.path, i.key AS attachment_key
                FROM itemAttachments ia
                JOIN items i ON ia.itemID = i.itemID
                WHERE ia.parentItemID = ?
                  AND ia.contentType = 'application/pdf'
                LIMIT 1
                """,
                (item_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None

            path_str: str = row["path"] or ""
            attachment_key: str = row["attachment_key"]

            if path_str.startswith("storage:"):
                filename = path_str[len("storage:") :]
                full_path = self.config.zotero_storage_dir / attachment_key / filename
                return full_path if full_path.exists() else None

            # Linked attachment — try as absolute path
            p = Path(path_str)
            return p if p.exists() else None
        finally:
            conn.close()

    def top_tags(self, limit: int = 30) -> list[tuple[str, int]]:
        """Return the most-used tags in the library as (name, usage_count) pairs.

        Used to anchor LLM tag suggestions to the user's existing tag vocabulary
        — without this, suggestions tend to invent synonyms (`ml` vs
        `machine-learning` vs `ML`) instead of reusing established tags.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT t.name, COUNT(*) AS uses
                FROM itemTags it
                JOIN tags t ON it.tagID = t.tagID
                WHERE it.itemID NOT IN (SELECT itemID FROM deletedItems)
                GROUP BY t.tagID
                ORDER BY uses DESC, t.name ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [(row["name"], row["uses"]) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_all_references(self) -> list[Reference]:
        """Return every non-deleted reference (used for vector indexing)."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT i.itemID, i.key AS item_key, it.typeName
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND it.typeName NOT IN ('attachment', 'note', 'annotation')
                """
            )
            return [
                self._build_reference(conn, row["itemID"], row["item_key"], row["typeName"])
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_reference(
        self, conn: sqlite3.Connection, item_id: int, item_key: str, type_name: str
    ) -> Reference:
        """Assemble a Reference from the normalised Zotero tables."""
        fields = self._get_fields(conn, item_id)
        creators = self._get_creators(conn, item_id)
        tags = self._get_tags(conn, item_id)

        return Reference(
            item_key=item_key,
            item_type=type_name,
            title=fields.get("title", ""),
            creators=creators,
            date=fields.get("date", ""),
            abstract=fields.get("abstractNote", ""),
            doi=fields.get("DOI", ""),
            url=fields.get("url", ""),
            publication=fields.get("publicationTitle", fields.get("bookTitle", "")),
            volume=fields.get("volume", ""),
            issue=fields.get("issue", ""),
            pages=fields.get("pages", ""),
            publisher=fields.get("publisher", ""),
            place=fields.get("place", ""),
            edition=fields.get("edition", ""),
            isbn=fields.get("ISBN", ""),
            issn=fields.get("ISSN", ""),
            tags=tags,
        )

    @staticmethod
    def _get_fields(conn: sqlite3.Connection, item_id: int) -> dict[str, str]:
        cursor = conn.execute(
            """
            SELECT f.fieldName, idv.value
            FROM itemData id
            JOIN fields f ON id.fieldID = f.fieldID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            WHERE id.itemID = ?
            """,
            (item_id,),
        )
        return {row["fieldName"]: row["value"] for row in cursor.fetchall()}

    @staticmethod
    def _get_creators(conn: sqlite3.Connection, item_id: int) -> list[dict[str, str]]:
        cursor = conn.execute(
            """
            SELECT c.firstName, c.lastName, ct.creatorType
            FROM itemCreators ic
            JOIN creators c ON ic.creatorID = c.creatorID
            JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
            WHERE ic.itemID = ?
            ORDER BY ic.orderIndex
            """,
            (item_id,),
        )
        return [
            {
                "firstName": row["firstName"] or "",
                "lastName": row["lastName"] or "",
                "creatorType": row["creatorType"],
            }
            for row in cursor.fetchall()
        ]

    @staticmethod
    def _get_tags(conn: sqlite3.Connection, item_id: int) -> list[str]:
        cursor = conn.execute(
            """
            SELECT t.name
            FROM itemTags it
            JOIN tags t ON it.tagID = t.tagID
            WHERE it.itemID = ?
            """,
            (item_id,),
        )
        return [row["name"] for row in cursor.fetchall()]
