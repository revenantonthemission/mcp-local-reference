"""Shared fixtures — notably a mock Zotero SQLite database."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from mcp_local_reference.config import Config

# ------------------------------------------------------------------
# Directories
# ------------------------------------------------------------------


@pytest.fixture()
def tmp_dir() -> Path:  # type: ignore[misc]
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ------------------------------------------------------------------
# Mock Zotero database
# ------------------------------------------------------------------

_SCHEMA_AND_SEED = """
CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
CREATE TABLE items (
    itemID INTEGER PRIMARY KEY, itemTypeID INT, libraryID INT DEFAULT 1,
    key TEXT UNIQUE, version INT DEFAULT 1
);
CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
CREATE TABLE itemData (
    itemID INT, fieldID INT, valueID INT,
    PRIMARY KEY (itemID, fieldID)
);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
CREATE TABLE itemCreators (
    itemID INT, creatorID INT, creatorTypeID INT DEFAULT 1,
    orderIndex INT DEFAULT 0,
    PRIMARY KEY (itemID, creatorID, creatorTypeID, orderIndex)
);
CREATE TABLE collections (
    collectionID INTEGER PRIMARY KEY, collectionName TEXT,
    parentCollectionID INT, libraryID INT DEFAULT 1, key TEXT UNIQUE
);
CREATE TABLE collectionItems (
    collectionID INT, itemID INT, orderIndex INT DEFAULT 0,
    PRIMARY KEY (collectionID, itemID)
);
CREATE TABLE itemAttachments (
    itemID INTEGER PRIMARY KEY, parentItemID INT,
    linkMode INT, contentType TEXT, charsetID INT, path TEXT
);
CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE itemTags (
    itemID INT, tagID INT, type INT DEFAULT 0,
    PRIMARY KEY (itemID, tagID)
);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);

-- Item types
INSERT INTO itemTypes VALUES (1, 'journalArticle');
INSERT INTO itemTypes VALUES (2, 'book');
INSERT INTO itemTypes VALUES (3, 'attachment');
INSERT INTO itemTypes VALUES (4, 'note');
INSERT INTO itemTypes VALUES (5, 'conferencePaper');

-- Fields
INSERT INTO fields VALUES (1, 'title');
INSERT INTO fields VALUES (2, 'abstractNote');
INSERT INTO fields VALUES (3, 'DOI');
INSERT INTO fields VALUES (4, 'date');
INSERT INTO fields VALUES (5, 'publicationTitle');
INSERT INTO fields VALUES (6, 'volume');
INSERT INTO fields VALUES (7, 'issue');
INSERT INTO fields VALUES (8, 'pages');
INSERT INTO fields VALUES (9, 'url');
INSERT INTO fields VALUES (10, 'publisher');
INSERT INTO fields VALUES (11, 'place');
INSERT INTO fields VALUES (12, 'edition');
INSERT INTO fields VALUES (13, 'ISBN');
INSERT INTO fields VALUES (14, 'ISSN');

-- Creator types
INSERT INTO creatorTypes VALUES (1, 'author');
INSERT INTO creatorTypes VALUES (2, 'editor');

-- ── Test article ──────────────────────────────────────────────────
INSERT INTO items VALUES (1, 1, 1, 'TESTKEY1', 1);

INSERT INTO itemDataValues VALUES (1, 'Deep Learning for Natural Language Processing');
INSERT INTO itemDataValues VALUES (2, 'A comprehensive review of deep learning approaches in NLP.');
INSERT INTO itemDataValues VALUES (3, '10.1234/test.2024');
INSERT INTO itemDataValues VALUES (4, '2024-01-15');
INSERT INTO itemDataValues VALUES (5, 'Journal of AI Research');
INSERT INTO itemDataValues VALUES (6, '42');
INSERT INTO itemDataValues VALUES (7, '3');
INSERT INTO itemDataValues VALUES (8, '100-150');

INSERT INTO itemData VALUES (1, 1, 1);
INSERT INTO itemData VALUES (1, 2, 2);
INSERT INTO itemData VALUES (1, 3, 3);
INSERT INTO itemData VALUES (1, 4, 4);
INSERT INTO itemData VALUES (1, 5, 5);
INSERT INTO itemData VALUES (1, 6, 6);
INSERT INTO itemData VALUES (1, 7, 7);
INSERT INTO itemData VALUES (1, 8, 8);

INSERT INTO creators VALUES (1, 'Alice', 'Smith');
INSERT INTO creators VALUES (2, 'Bob', 'Jones');
INSERT INTO itemCreators VALUES (1, 1, 1, 0);
INSERT INTO itemCreators VALUES (1, 2, 1, 1);

INSERT INTO tags VALUES (1, 'deep-learning');
INSERT INTO tags VALUES (2, 'nlp');
INSERT INTO itemTags VALUES (1, 1, 0);
INSERT INTO itemTags VALUES (1, 2, 0);

-- ── Test book ─────────────────────────────────────────────────────
INSERT INTO items VALUES (2, 2, 1, 'TESTKEY2', 1);

INSERT INTO itemDataValues VALUES (9, 'Artificial Intelligence: A Modern Approach');
INSERT INTO itemDataValues VALUES (10, '2021');
INSERT INTO itemDataValues VALUES (11, 'Pearson');
INSERT INTO itemDataValues VALUES (12, 'Hoboken, NJ');
INSERT INTO itemDataValues VALUES (13, '4th edn');

INSERT INTO itemData VALUES (2, 1, 9);
INSERT INTO itemData VALUES (2, 4, 10);
INSERT INTO itemData VALUES (2, 10, 11);
INSERT INTO itemData VALUES (2, 11, 12);
INSERT INTO itemData VALUES (2, 12, 13);

INSERT INTO creators VALUES (3, 'Stuart', 'Russell');
INSERT INTO creators VALUES (4, 'Peter', 'Norvig');
INSERT INTO itemCreators VALUES (2, 3, 1, 0);
INSERT INTO itemCreators VALUES (2, 4, 1, 1);

-- ── Collection ────────────────────────────────────────────────────
INSERT INTO collections VALUES (1, 'Machine Learning', NULL, 1, 'COLL1');
INSERT INTO collectionItems VALUES (1, 1, 0);
INSERT INTO collectionItems VALUES (1, 2, 1);
"""


@pytest.fixture()
def mock_zotero_db(tmp_dir: Path) -> Path:
    """Create a minimal Zotero SQLite database populated with test data."""
    db_path = tmp_dir / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_AND_SEED)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def config(mock_zotero_db: Path, tmp_dir: Path) -> Config:
    """Return a Config pointing at the mock Zotero database."""
    return Config(
        zotero_data_dir=mock_zotero_db.parent,
        data_dir=tmp_dir / "mcp-data",
    )
