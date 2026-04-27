# Collection Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 7 MCP tools for editing Zotero collections — 4 lifecycle (create / rename / reparent / delete), 2 item-membership (add / remove items, per collection), and 1 read-only `suggest_collection_placement`. Builds on the `apply_tags` / `remove_tags` write-path pattern (dry-run default, 25-item cap, optimistic concurrency).

**Architecture:** Single new tool module `src/mcp_local_reference/tools/collections.py` (Approach 1 from the spec). Extends the existing `ZoteroApiClient` with collection-object methods plus `update_item_collections` (the per-item membership write). Item-membership tools issue **one PATCH per item** (so a 25-item batch = 25 PATCHes) with **partial-failure semantics**. All other constraints inherit from `auto_tag.py`: `*_impl()` helpers outside the closure, `dry_run=True` default, `If-Unmodified-Since-Version`, dry-run reads local SQLite.

**Tech Stack:** Python 3.11+, FastMCP, pydantic-settings, httpx (test layer uses `httpx.MockTransport`), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-27-collection-editing-design.md`

**Branch:** `feat/collection-editing` (already created; spec already committed at `61b5f26`).

---

## Reference: Existing helpers being reused

These are already in the codebase. Engineers reading tasks out of order will find them in the listed locations.

- **`MAX_TAGS_PER_CALL = 25`** (`auto_tag.py`) — model for the new `MAX_ITEMS_PER_CALL = 25` constant in `collections.py`.
- **`_local_snapshot(zotero, item_key) -> ItemSnapshot | None`** (`auto_tag.py`) — model for the new `_local_collection_snapshot(zotero, collection_key)` helper.
- **`ItemSnapshot`** (dataclass in `services/zotero_api_client.py`) — already has `item_key, version, tags, raw`. Task 1 adds a `collections: list[str]` field.
- **`ZoteroApiClient.get_item / set_tags`** — already exist; we add 5 sibling methods.
- **`_FakeApi` / `_FakeZotero` / `_ref` / `api_config`** (in `tests/test_auto_tag.py` and `tests/conftest.py`) — model for the new collection-aware fakes that will live in `tests/test_collections.py`.
- **`mock_zotero_db`** fixture (`tests/conftest.py`) — populates a SQLite file with a tiny Zotero schema. Task 6 needs to add `collections` and `collectionItems` rows for `get_item_collections` and tool-level integration tests.
- **`TestZoteroApiClient` class in `tests/test_auto_tag.py`** — the spec referred to `tests/test_zotero_api_client.py` (which doesn't exist in this repo). The new HTTP-layer tests in this plan extend the existing class in `test_auto_tag.py`. No file split required.

---

### Task 1: Extend `ItemSnapshot` with a `collections` field

Backward-compatible additive change. The Web API's item record includes `collections: [<key>, ...]`; we capture it so item-membership tools can compute set-union/set-difference without an extra fetch.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_api_client.py`
- Modify: `tests/test_auto_tag.py` (extend `TestZoteroApiClient.test_get_item_returns_snapshot`)

- [ ] **Step 1: Extend the existing `test_get_item_returns_snapshot` test**

In `tests/test_auto_tag.py`, find `test_get_item_returns_snapshot` (around line 67). Replace its body with the version below — adds `collections` to the JSON payload and asserts the new field is captured:

```python
    def test_get_item_returns_snapshot(self, api_config: Config) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["api_key"] = request.headers.get("Zotero-API-Key", "")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "version": 17,
                        "tags": [{"tag": "ml"}, {"tag": "papers"}],
                        "collections": ["COLLAAA1", "COLLBBB2"],
                    }
                },
            )

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        snap = client.get_item("ABCD1234")

        assert snap.version == 17
        assert snap.tags == ["ml", "papers"]
        assert snap.collections == ["COLLAAA1", "COLLBBB2"]
        assert "users/42/items/ABCD1234" in captured["url"]
        assert captured["api_key"] == "test-key"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient::test_get_item_returns_snapshot -v`
Expected: FAIL with `AttributeError: 'ItemSnapshot' object has no attribute 'collections'`.

- [ ] **Step 3: Add the `collections` field to `ItemSnapshot`**

In `src/mcp_local_reference/services/zotero_api_client.py`, modify the `ItemSnapshot` dataclass (around line 31):

```python
@dataclass
class ItemSnapshot:
    """Minimal view of a Zotero item — what we need for tag merging."""

    item_key: str
    version: int
    tags: list[str]
    collections: list[str]
    raw: dict[str, Any]
```

In the same file, modify `get_item` to populate the new field (around line 73):

```python
        return ItemSnapshot(
            item_key=item_key,
            version=int(data.get("version", 0)),
            tags=[t["tag"] for t in data.get("tags", []) if "tag" in t],
            collections=list(data.get("collections", [])),
            raw=data,
        )
```

- [ ] **Step 4: Update the `_local_snapshot` helper in `auto_tag.py`**

`auto_tag.py`'s `_local_snapshot` builds an `ItemSnapshot` with hard-coded fields. The dataclass now requires `collections`. Find `_local_snapshot` in `src/mcp_local_reference/tools/auto_tag.py` and add `collections=[]` to the `ItemSnapshot(...)` call. The local SQLite read for tags only needs to know tags; collections are unused on the dry-run side of `apply_tags` / `remove_tags`, so an empty list is correct.

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -v`
Expected: PASS — all 150 existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_local_reference/services/zotero_api_client.py \
        src/mcp_local_reference/tools/auto_tag.py \
        tests/test_auto_tag.py
git commit -m "$(cat <<'EOF'
feat(api): add collections field to ItemSnapshot

Backward-compatible additive field populated from the Web API's item
data. Needed by upcoming collection-editing tools to compute set-union
and set-difference on per-item collection membership.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Add `CollectionSnapshot` dataclass and `_UNSET` sentinel

These are pure data scaffolding; no API calls yet. Tested by an import-and-instantiate smoke test.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_api_client.py`
- Modify: `tests/test_auto_tag.py` (extend `TestZoteroApiClient`)

- [ ] **Step 1: Write the failing test**

Append to `TestZoteroApiClient` in `tests/test_auto_tag.py`:

```python
    def test_collection_snapshot_dataclass_shape(self) -> None:
        from mcp_local_reference.services.zotero_api_client import CollectionSnapshot

        snap = CollectionSnapshot(
            collection_key="COLL1234",
            version=42,
            name="AI",
            parent_key=None,
            raw={"key": "COLL1234"},
        )
        assert snap.collection_key == "COLL1234"
        assert snap.version == 42
        assert snap.name == "AI"
        assert snap.parent_key is None
        assert snap.raw == {"key": "COLL1234"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient::test_collection_snapshot_dataclass_shape -v`
Expected: FAIL with `ImportError: cannot import name 'CollectionSnapshot'`.

- [ ] **Step 3: Add the dataclass and sentinel**

In `src/mcp_local_reference/services/zotero_api_client.py`, after the existing `ItemSnapshot` dataclass and before `class ZoteroApiClient`, add:

```python
@dataclass
class CollectionSnapshot:
    """Minimal view of a Zotero collection — what we need for lifecycle edits."""

    collection_key: str
    version: int
    name: str
    parent_key: str | None  # None at root
    raw: dict[str, Any]


class _Sentinel:
    """Marker type for arguments that distinguish 'unset' from 'set to None'."""


_UNSET: _Sentinel = _Sentinel()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient::test_collection_snapshot_dataclass_shape -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
git commit -m "$(cat <<'EOF'
feat(api): add CollectionSnapshot dataclass and _UNSET sentinel

Scaffolding for the upcoming collection-object client methods.
_UNSET supports three-state semantics on update_collection's
parent_key argument (omitted vs explicit None vs string).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `ZoteroApiClient.create_collection` + `get_collection`

POST `/collections` and GET `/collections/<key>`. Both straightforward; tested via `httpx.MockTransport`.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_api_client.py`
- Modify: `tests/test_auto_tag.py` (extend `TestZoteroApiClient`)

- [ ] **Step 1: Write the failing tests**

Append to `TestZoteroApiClient` in `tests/test_auto_tag.py`:

```python
    def test_create_collection_posts_and_returns_snapshot(
        self, api_config: Config
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "successful": {
                        "0": {
                            "key": "NEWKEYAA",
                            "version": 5,
                            "data": {
                                "key": "NEWKEYAA",
                                "version": 5,
                                "name": "AI",
                                "parentCollection": "PARENTKK",
                            },
                        }
                    },
                    "success": {"0": "NEWKEYAA"},
                    "failed": {},
                    "unchanged": {},
                },
            )

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        snap = client.create_collection("AI", parent_key="PARENTKK")

        assert snap.collection_key == "NEWKEYAA"
        assert snap.version == 5
        assert snap.name == "AI"
        assert snap.parent_key == "PARENTKK"
        assert captured["method"] == "POST"
        assert "users/42/collections" in str(captured["url"])
        assert captured["body"] == [{"name": "AI", "parentCollection": "PARENTKK"}]

    def test_create_collection_at_root_sends_false_parent(
        self, api_config: Config
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "successful": {
                        "0": {
                            "key": "ROOTKEYY",
                            "version": 1,
                            "data": {
                                "key": "ROOTKEYY",
                                "version": 1,
                                "name": "TopLevel",
                                "parentCollection": False,
                            },
                        }
                    },
                    "success": {"0": "ROOTKEYY"},
                    "failed": {},
                    "unchanged": {},
                },
            )

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        snap = client.create_collection("TopLevel", parent_key=None)

        assert snap.parent_key is None
        assert captured["body"] == [{"name": "TopLevel", "parentCollection": False}]

    def test_get_collection_returns_snapshot(self, api_config: Config) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "key": "COLL1234",
                        "version": 9,
                        "name": "Sinology",
                        "parentCollection": False,
                    }
                },
            )

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        snap = client.get_collection("COLL1234")
        assert snap.collection_key == "COLL1234"
        assert snap.version == 9
        assert snap.name == "Sinology"
        assert snap.parent_key is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient -k "create_collection or get_collection" -v`
Expected: FAIL — `AttributeError: 'ZoteroApiClient' object has no attribute 'create_collection'`.

- [ ] **Step 3: Implement `get_collection` and `create_collection`**

In `src/mcp_local_reference/services/zotero_api_client.py`, add a private URL helper and the two new methods. Place them after `set_tags` and before the `# ---- Internal ----` section:

```python
    # ------------------------------------------------------------------
    # Collection-object operations
    # ------------------------------------------------------------------

    def get_collection(self, collection_key: str) -> CollectionSnapshot:
        """Fetch a single collection; needed before any write to capture its version."""
        self._require_credentials()
        url = self._collection_url(collection_key)
        with self._client(self._headers()) as client:
            response = client.get(url)
        if response.status_code == 404:
            raise ZoteroApiError(
                f"Collection '{collection_key}' not found in Zotero Web API"
            )
        response.raise_for_status()
        body = response.json()
        data = body.get("data", body)
        return self._collection_snapshot_from_data(data)

    def create_collection(
        self, name: str, parent_key: str | None
    ) -> CollectionSnapshot:
        """POST a new collection; returns a snapshot of the created collection."""
        self._require_credentials()
        url = self._collections_url()
        payload: dict[str, Any] = {"name": name}
        payload["parentCollection"] = parent_key if parent_key is not None else False
        with self._client(self._headers()) as client:
            response = client.post(url, json=[payload])
        response.raise_for_status()
        body = response.json()
        if body.get("failed"):
            raise ZoteroApiError(
                f"Zotero rejected create_collection: {body['failed']}"
            )
        try:
            entry = body["successful"]["0"]
        except (KeyError, TypeError) as exc:
            raise ZoteroApiError(
                f"Unexpected create_collection response: {body!r}"
            ) from exc
        return self._collection_snapshot_from_data(entry["data"])
```

Add the URL helpers and the snapshot-builder near the existing `_item_url` (around line 115):

```python
    def _collections_url(self) -> str:
        base = self.config.zotero_api_base_url.rstrip("/")
        return f"{base}/users/{self.config.zotero_user_id}/collections"

    def _collection_url(self, collection_key: str) -> str:
        return f"{self._collections_url()}/{collection_key}"

    @staticmethod
    def _collection_snapshot_from_data(data: dict[str, Any]) -> CollectionSnapshot:
        parent_raw = data.get("parentCollection")
        parent_key = parent_raw if isinstance(parent_raw, str) else None
        return CollectionSnapshot(
            collection_key=data["key"],
            version=int(data.get("version", 0)),
            name=data.get("name", ""),
            parent_key=parent_key,
            raw=data,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient -k "create_collection or get_collection" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
git commit -m "$(cat <<'EOF'
feat(api): add create_collection and get_collection to ZoteroApiClient

POST /collections and GET /collections/<key>. parent_key=None becomes
parentCollection: false on the wire (Zotero's representation for
"library root").

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `ZoteroApiClient.update_collection` (rename, reparent, both)

PATCH `/collections/<key>`. Three-state `parent_key` argument via the `_UNSET` sentinel.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_api_client.py`
- Modify: `tests/test_auto_tag.py` (extend `TestZoteroApiClient`)

- [ ] **Step 1: Write the failing tests**

Append to `TestZoteroApiClient` in `tests/test_auto_tag.py`:

```python
    def test_update_collection_rename_only(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["unmod"] = request.headers.get("If-Unmodified-Since-Version")
            captured["body"] = json.loads(request.content)
            return httpx.Response(204, headers={"Last-Modified-Version": "11"})

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        new_version = client.update_collection(
            "COLL1234", name="Artificial Intelligence", version=10
        )

        assert new_version == 11
        assert captured["method"] == "PATCH"
        assert captured["unmod"] == "10"
        assert captured["body"] == {"name": "Artificial Intelligence"}

    def test_update_collection_reparent_to_root(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(204, headers={"Last-Modified-Version": "12"})

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        client.update_collection("COLL1234", parent_key=None, version=11)

        assert captured["body"] == {"parentCollection": False}

    def test_update_collection_reparent_to_other(self, api_config: Config) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(204, headers={"Last-Modified-Version": "13"})

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        client.update_collection("COLL1234", parent_key="NEWPARNT", version=12)

        assert captured["body"] == {"parentCollection": "NEWPARNT"}

    def test_update_collection_412_raises_conflict(self, api_config: Config) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(412))
        client = ZoteroApiClient(api_config, transport=transport)
        with pytest.raises(VersionConflictError):
            client.update_collection("COLL1234", name="X", version=10)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient -k update_collection -v`
Expected: FAIL — `AttributeError: ... no attribute 'update_collection'`.

- [ ] **Step 3: Implement `update_collection`**

In `src/mcp_local_reference/services/zotero_api_client.py`, add to the "Collection-object operations" section (after `create_collection`):

```python
    def update_collection(
        self,
        collection_key: str,
        *,
        name: str | None = None,
        parent_key: str | None | _Sentinel = _UNSET,
        version: int,
    ) -> int:
        """PATCH a collection's name and/or parent.

        ``parent_key`` semantics:
          - ``_UNSET`` (default) — don't touch parent; rename only.
          - explicit ``None`` — move to library root (Zotero: parentCollection=false).
          - explicit ``str`` — move under that collection.

        Uses ``If-Unmodified-Since-Version`` for optimistic concurrency.
        """
        self._require_credentials()
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if not isinstance(parent_key, _Sentinel):
            body["parentCollection"] = parent_key if parent_key is not None else False
        if not body:
            return version  # no-op
        url = self._collection_url(collection_key)
        headers = {**self._headers(), "If-Unmodified-Since-Version": str(version)}
        with self._client(headers) as client:
            response = client.patch(url, json=body)
        if response.status_code == 412:
            raise VersionConflictError(
                f"Collection '{collection_key}' was modified since version {version}; "
                "refetch and retry"
            )
        if response.status_code == 404:
            raise ZoteroApiError(f"Collection '{collection_key}' not found")
        response.raise_for_status()
        new_version = response.headers.get("Last-Modified-Version")
        return int(new_version) if new_version else version + 1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient -k update_collection -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
git commit -m "$(cat <<'EOF'
feat(api): add update_collection to ZoteroApiClient

PATCH /collections/<key> with optional name and parent_key updates.
Three-state parent_key via _UNSET sentinel: omitted (don't touch),
explicit None (move to root), explicit str (re-parent).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `ZoteroApiClient.delete_collection`

DELETE `/collections/<key>` with `If-Unmodified-Since-Version`.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_api_client.py`
- Modify: `tests/test_auto_tag.py` (extend `TestZoteroApiClient`)

- [ ] **Step 1: Write the failing test**

Append to `TestZoteroApiClient`:

```python
    def test_delete_collection_sends_delete_with_version(
        self, api_config: Config
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["unmod"] = request.headers.get("If-Unmodified-Since-Version")
            return httpx.Response(204)

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        client.delete_collection("COLL1234", version=15)

        assert captured["method"] == "DELETE"
        assert "users/42/collections/COLL1234" in str(captured["url"])
        assert captured["unmod"] == "15"

    def test_delete_collection_412_raises_conflict(self, api_config: Config) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(412))
        client = ZoteroApiClient(api_config, transport=transport)
        with pytest.raises(VersionConflictError):
            client.delete_collection("COLL1234", version=15)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient -k delete_collection -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement `delete_collection`**

In `src/mcp_local_reference/services/zotero_api_client.py`, add to the "Collection-object operations" section (after `update_collection`):

```python
    def delete_collection(self, collection_key: str, version: int) -> None:
        """DELETE a collection. Items lose this membership but are not deleted."""
        self._require_credentials()
        url = self._collection_url(collection_key)
        headers = {**self._headers(), "If-Unmodified-Since-Version": str(version)}
        with self._client(headers) as client:
            response = client.delete(url)
        if response.status_code == 412:
            raise VersionConflictError(
                f"Collection '{collection_key}' was modified since version {version}; "
                "refetch and retry"
            )
        if response.status_code == 404:
            raise ZoteroApiError(f"Collection '{collection_key}' not found")
        response.raise_for_status()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient -k delete_collection -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
git commit -m "$(cat <<'EOF'
feat(api): add delete_collection to ZoteroApiClient

DELETE /collections/<key> with If-Unmodified-Since-Version. Zotero's
behavior: items lose this membership but remain; sub-collections are
orphaned (their parentCollection points to nothing).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `ZoteroApiClient.update_item_collections`

PATCH `/items/<key>` with the full `collections` array. Sibling of `set_tags`.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_api_client.py`
- Modify: `tests/test_auto_tag.py` (extend `TestZoteroApiClient`)

- [ ] **Step 1: Write the failing test**

Append to `TestZoteroApiClient`:

```python
    def test_update_item_collections_sends_patch_with_version(
        self, api_config: Config
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["unmod"] = request.headers.get("If-Unmodified-Since-Version")
            captured["body"] = json.loads(request.content)
            return httpx.Response(204, headers={"Last-Modified-Version": "21"})

        client = ZoteroApiClient(api_config, transport=httpx.MockTransport(handler))
        new_version = client.update_item_collections(
            "ITEMAAAA", ["COLL1111", "COLL2222"], version=20
        )

        assert new_version == 21
        assert captured["method"] == "PATCH"
        assert "users/42/items/ITEMAAAA" in str(captured["url"])
        assert captured["unmod"] == "20"
        assert captured["body"] == {"collections": ["COLL1111", "COLL2222"]}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient::test_update_item_collections_sends_patch_with_version -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement `update_item_collections`**

In `src/mcp_local_reference/services/zotero_api_client.py`, after `set_tags` (still inside the "Public API" section, before the collection methods if you like — placement is style; keep it next to `set_tags` since they're sibling item-record writes):

```python
    def update_item_collections(
        self,
        item_key: str,
        collection_keys: list[str],
        version: int,
    ) -> int:
        """PATCH the item's `collections` array; returns the new item version.

        Sibling of `set_tags`: same endpoint pattern, same concurrency
        story (If-Unmodified-Since-Version). Pass the FULL desired list,
        not a diff — Zotero's PATCH replaces the array.
        """
        self._require_credentials()
        url = self._item_url(item_key)
        headers = {**self._headers(), "If-Unmodified-Since-Version": str(version)}
        body = {"collections": list(collection_keys)}
        with self._client(headers) as client:
            response = client.patch(url, json=body)
        if response.status_code == 412:
            raise VersionConflictError(
                f"Item '{item_key}' was modified since version {version}; "
                "refetch and retry"
            )
        if response.status_code == 404:
            raise ZoteroApiError(f"Item '{item_key}' not found")
        response.raise_for_status()
        new_version = response.headers.get("Last-Modified-Version")
        return int(new_version) if new_version else version + 1
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_auto_tag.py::TestZoteroApiClient::test_update_item_collections_sends_patch_with_version -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS — 150 prior + 11 new (Tasks 1–6) = 161 tests.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_local_reference/services/zotero_api_client.py tests/test_auto_tag.py
git commit -m "$(cat <<'EOF'
feat(api): add update_item_collections to ZoteroApiClient

Sibling of set_tags: PATCH /items/<key> with the full collections
array. Item-membership tools will call this once per item in a batch
(per-item version checks → partial-failure semantics).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `ZoteroClient.get_item_collections`

Local SQLite read. Needed for dry-run partition in the membership tools.

**Files:**
- Modify: `src/mcp_local_reference/services/zotero_client.py`
- Modify: `tests/conftest.py` (extend `mock_zotero_db` to populate collections + collectionItems)
- Modify: `tests/test_zotero_client.py`

- [ ] **Step 1: Extend `mock_zotero_db` to seed collections**

Find `mock_zotero_db` in `tests/conftest.py`. After the existing schema/data setup, add (just before the function returns the Path) — adapt to the existing variable names; the goal is two collections, one item assigned to one of them:

```python
    # Collection schema + sample data for collection-editing tests
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS collections (
            collectionID INTEGER PRIMARY KEY,
            collectionName TEXT NOT NULL,
            key TEXT NOT NULL UNIQUE,
            parentCollectionID INTEGER REFERENCES collections(collectionID)
        );
        CREATE INDEX IF NOT EXISTS idx_collections_parent
            ON collections(parentCollectionID);

        CREATE TABLE IF NOT EXISTS collectionItems (
            collectionID INTEGER NOT NULL REFERENCES collections(collectionID),
            itemID INTEGER NOT NULL REFERENCES items(itemID),
            orderIndex INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (collectionID, itemID)
        );
        CREATE INDEX IF NOT EXISTS idx_collectionItems_item
            ON collectionItems(itemID);
        """
    )
    cur.execute(
        "INSERT INTO collections (collectionID, collectionName, key) VALUES (1, 'AI', 'COLLAI11')"
    )
    cur.execute(
        "INSERT INTO collections (collectionID, collectionName, key, parentCollectionID) "
        "VALUES (2, 'LLMs', 'COLLLM22', 1)"
    )
    # Assume the existing fixture inserted at least one item with itemID=1.
    cur.execute(
        "INSERT INTO collectionItems (collectionID, itemID, orderIndex) VALUES (1, 1, 0)"
    )
    conn.commit()
```

If the existing `mock_zotero_db` doesn't expose `cur` / `conn` at the bottom, restructure so the connection stays open through this block (the schema for `items` is already set up earlier in the function). If the existing fixture's first item has a different `itemID`, change the `INSERT INTO collectionItems` accordingly.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_zotero_client.py`:

```python
class TestGetItemCollections:
    def test_returns_keys_for_item_in_one_collection(self, config: Config) -> None:
        from mcp_local_reference.services.zotero_client import ZoteroClient

        client = ZoteroClient(config)
        # The first seeded item is in COLLAI11 only (per conftest).
        seeded_key = client.search("", limit=1)[0].item_key
        keys = client.get_item_collections(seeded_key)
        assert keys == ["COLLAI11"]

    def test_returns_empty_for_unfiled_item(self, config: Config) -> None:
        from mcp_local_reference.services.zotero_client import ZoteroClient

        client = ZoteroClient(config)
        keys = client.get_item_collections("NOSUCHKK")
        assert keys == []
```

(If `client.search("")` doesn't return results, replace with whatever item-fetch the existing tests use — the goal is to grab a known `item_key` from the fixture.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_zotero_client.py::TestGetItemCollections -v`
Expected: FAIL — `AttributeError: 'ZoteroClient' object has no attribute 'get_item_collections'`.

- [ ] **Step 4: Implement `get_item_collections`**

In `src/mcp_local_reference/services/zotero_client.py`, add a new method after `get_collection_items` (around line 200):

```python
    def get_item_collections(self, item_key: str) -> list[str]:
        """Return the collection keys an item currently belongs to."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                SELECT c.key
                FROM collections c
                JOIN collectionItems ci ON c.collectionID = ci.collectionID
                JOIN items i ON ci.itemID = i.itemID
                WHERE i.key = ?
                ORDER BY c.key
                """,
                (item_key,),
            )
            return [row["key"] for row in cursor.fetchall()]
        finally:
            conn.close()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_zotero_client.py::TestGetItemCollections -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS — 161 + 2 = 163 tests.

- [ ] **Step 7: Commit**

```bash
git add src/mcp_local_reference/services/zotero_client.py \
        tests/conftest.py tests/test_zotero_client.py
git commit -m "$(cat <<'EOF'
feat(client): add get_item_collections read helper

Reads the collection keys an item currently belongs to from the local
SQLite. Used by upcoming membership tools to compute would_add /
already_present partitions in dry-run without contacting the Web API.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Skeleton tool module + private helpers + test scaffolding

Creates `tools/collections.py` with the module-level constant, the three private helpers (`_local_collection_snapshot`, `_walk_descendants`, `_check_cycle`), the empty `register_tools` function, and the new `tests/test_collections.py` with shared fakes. No tools yet — they come in subsequent tasks. Wires `register_tools` into `server.py`.

**Files:**
- Create: `src/mcp_local_reference/tools/collections.py`
- Create: `tests/test_collections.py`
- Modify: `src/mcp_local_reference/server.py`

- [ ] **Step 1: Write the failing test for `_check_cycle`**

Create `tests/test_collections.py`:

```python
"""Tests for the collection-editing tools and their private helpers.

Mirrors the structure of test_auto_tag.py: HTTP-layer tests live with
their existing client class in test_auto_tag.py; this file holds tool-
orchestration tests via a small _FakeApi / _FakeZotero pair.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from mcp_local_reference.services.zotero_api_client import (
    CollectionSnapshot,
    ItemSnapshot,
    MissingCredentialsError,
    VersionConflictError,
    ZoteroApiError,
)
from mcp_local_reference.services.zotero_client import Collection, Reference
from mcp_local_reference.tools.collections import (
    MAX_ITEMS_PER_CALL,
    _check_cycle,
    _local_collection_snapshot,
)


# ======================================================================
# Fakes — collection-aware stand-ins for ZoteroApiClient and ZoteroClient
# ======================================================================


class _FakeApi:
    """Stand-in for ZoteroApiClient covering all 7 methods used by the tools."""

    def __init__(
        self,
        item_snapshots: dict[str, ItemSnapshot] | None = None,
        collection_snapshots: dict[str, CollectionSnapshot] | None = None,
        get_item_error: Exception | None = None,
        get_collection_error: Exception | None = None,
        update_item_error: Exception | None = None,
        update_collection_error: Exception | None = None,
        create_collection_error: Exception | None = None,
        delete_collection_error: Exception | None = None,
        new_version: int = 99,
        created_snapshot: CollectionSnapshot | None = None,
    ) -> None:
        self.item_snapshots = item_snapshots or {}
        self.collection_snapshots = collection_snapshots or {}
        self.get_item_error = get_item_error
        self.get_collection_error = get_collection_error
        self.update_item_error = update_item_error
        self.update_collection_error = update_collection_error
        self.create_collection_error = create_collection_error
        self.delete_collection_error = delete_collection_error
        self.new_version = new_version
        self.created_snapshot = created_snapshot
        self.update_item_calls: list[tuple[str, list[str], int]] = []
        self.update_collection_calls: list[tuple[str, dict, int]] = []
        self.create_collection_calls: list[tuple[str, str | None]] = []
        self.delete_collection_calls: list[tuple[str, int]] = []

    def get_item(self, item_key: str) -> ItemSnapshot:
        if self.get_item_error is not None:
            raise self.get_item_error
        return self.item_snapshots[item_key]

    def get_collection(self, key: str) -> CollectionSnapshot:
        if self.get_collection_error is not None:
            raise self.get_collection_error
        return self.collection_snapshots[key]

    def update_item_collections(
        self, item_key: str, collection_keys: list[str], version: int
    ) -> int:
        self.update_item_calls.append((item_key, list(collection_keys), version))
        if self.update_item_error is not None:
            raise self.update_item_error
        return self.new_version

    def update_collection(self, collection_key: str, *, version: int, **kwargs) -> int:
        self.update_collection_calls.append((collection_key, dict(kwargs), version))
        if self.update_collection_error is not None:
            raise self.update_collection_error
        return self.new_version

    def create_collection(self, name: str, parent_key: str | None) -> CollectionSnapshot:
        self.create_collection_calls.append((name, parent_key))
        if self.create_collection_error is not None:
            raise self.create_collection_error
        assert self.created_snapshot is not None
        return self.created_snapshot

    def delete_collection(self, collection_key: str, version: int) -> None:
        self.delete_collection_calls.append((collection_key, version))
        if self.delete_collection_error is not None:
            raise self.delete_collection_error


class _FakeZotero:
    """Stand-in for ZoteroClient — only the methods tools actually call."""

    def __init__(
        self,
        references: dict[str, Reference] | None = None,
        collections: list[Collection] | None = None,
        item_collections: dict[str, list[str]] | None = None,
        items_per_collection: dict[str, list[str]] | None = None,
    ) -> None:
        self.references = references or {}
        self.collections = collections or []
        self.item_collections = item_collections or {}
        self.items_per_collection = items_per_collection or {}

    def get_reference(self, item_key: str) -> Reference | None:
        return self.references.get(item_key)

    def list_collections(self) -> list[Collection]:
        return self.collections

    def get_item_collections(self, item_key: str) -> list[str]:
        return list(self.item_collections.get(item_key, []))

    def get_collection_items(self, collection_key: str, limit: int = 50):
        keys = self.items_per_collection.get(collection_key, [])
        return [self.references[k] for k in keys if k in self.references]


def _coll(key: str, name: str, parent: str | None = None) -> Collection:
    return Collection(key=key, name=name, parent_key=parent)


def _ref(item_key: str, title: str = "", abstract: str = "") -> Reference:
    return Reference(
        item_key=item_key,
        item_type="journalArticle",
        title=title,
        abstract=abstract,
        tags=[],
    )


# ======================================================================
# _check_cycle — local DAG walk used by reparent_collection
# ======================================================================


class TestCheckCycle:
    def test_no_cycle_for_unrelated_collection(self) -> None:
        # AI (no parent), Sinology (no parent). Reparent AI under Sinology — fine.
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI"), _coll("SINK2222", "Sinology")]
        )
        assert _check_cycle(zotero, target_key="AIK11111", new_parent_key="SINK2222") is False

    def test_cycle_when_new_parent_is_target(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        assert _check_cycle(zotero, target_key="AIK11111", new_parent_key="AIK11111") is True

    def test_cycle_when_new_parent_is_descendant(self) -> None:
        # AI -> LLMs -> Transformers; reparent AI under Transformers => cycle.
        zotero = _FakeZotero(
            collections=[
                _coll("AIK11111", "AI"),
                _coll("LLMK2222", "LLMs", "AIK11111"),
                _coll("TRMK3333", "Transformers", "LLMK2222"),
            ]
        )
        assert _check_cycle(zotero, target_key="AIK11111", new_parent_key="TRMK3333") is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_collections.py -v`
Expected: FAIL — `ImportError: cannot import name 'MAX_ITEMS_PER_CALL' ... 'mcp_local_reference.tools.collections'`.

- [ ] **Step 3: Create the tool module skeleton**

Create `src/mcp_local_reference/tools/collections.py`:

```python
"""Collection-editing MCP tools.

Seven tools mirror the auto_tag.py pattern but operate on Zotero
collections instead of tags. Six write tools default to dry_run=True;
the seventh (suggest_collection_placement) is read-only and gathers
context for Claude to advise on placement without writing.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_local_reference.config import Config
from mcp_local_reference.services.pdf_processor import PdfProcessor
from mcp_local_reference.services.zotero_api_client import (
    CollectionSnapshot,
    ItemSnapshot,
    MissingCredentialsError,
    VersionConflictError,
    ZoteroApiClient,
    ZoteroApiError,
)
from mcp_local_reference.services.zotero_client import Collection, ZoteroClient

MAX_ITEMS_PER_CALL = 25


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register collection-editing tools on *mcp*."""
    zotero = ZoteroClient(config)
    api = ZoteroApiClient(config)
    pdf = PdfProcessor(min_figure_pixels=config.min_figure_pixels)

    # Tool registration is added in Tasks 9–15. Each registers one tool.
    # The api/zotero/pdf instances above are passed into each `_register_*`.
    _ = (api, zotero, pdf)  # placeholder until tools are wired in


# ----------------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------------


def _local_collection_snapshot(
    zotero: ZoteroClient, collection_key: str
) -> CollectionSnapshot | None:
    """Build a CollectionSnapshot from local SQLite, or return None if missing.

    Mirrors `_local_snapshot` in auto_tag.py: version=0 is intentionally
    invalid so a local snapshot accidentally fed to a Web API write
    fails loudly with HTTP 412 instead of silently corrupting.
    """
    for col in _flatten_tree(zotero.list_collections()):
        if col.key == collection_key:
            return CollectionSnapshot(
                collection_key=col.key,
                version=0,
                name=col.name,
                parent_key=col.parent_key,
                raw={},
            )
    return None


def _flatten_tree(roots: list[Collection]) -> list[Collection]:
    """Walk the nested collection tree and return every node as a flat list."""
    out: list[Collection] = []
    stack: list[Collection] = list(roots)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


def _walk_descendants(zotero: ZoteroClient, root_key: str) -> set[str]:
    """Return the keys of every descendant of root_key (excluding root_key itself)."""
    all_cols = _flatten_tree(zotero.list_collections())
    children_of: dict[str, list[str]] = {}
    for c in all_cols:
        if c.parent_key:
            children_of.setdefault(c.parent_key, []).append(c.key)
    descendants: set[str] = set()
    stack: list[str] = list(children_of.get(root_key, []))
    while stack:
        node = stack.pop()
        if node in descendants:
            continue
        descendants.add(node)
        stack.extend(children_of.get(node, []))
    return descendants


def _check_cycle(zotero: ZoteroClient, target_key: str, new_parent_key: str) -> bool:
    """Return True if reparenting `target_key` under `new_parent_key` would cycle."""
    if target_key == new_parent_key:
        return True
    return new_parent_key in _walk_descendants(zotero, target_key)


def _err(message: str) -> str:
    return json.dumps({"error": message})
```

- [ ] **Step 4: Wire `register_tools` into `server.py`**

In `src/mcp_local_reference/server.py`:

```python
from mcp_local_reference.tools import (
    auto_tag,
    collections as collections_tools,
    figures,
    local_pdf,
    pdf_reader,
    references,
)
```

…and inside `create_server`, after `auto_tag.register_tools(mcp, config)`:

```python
    collections_tools.register_tools(mcp, config)
```

(Use `collections as collections_tools` to avoid shadowing the standard-library name.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_collections.py -v`
Expected: PASS (3 tests in `TestCheckCycle`).

- [ ] **Step 6: Smoke-test server startup**

Run: `uv run python -c "from mcp_local_reference.server import create_server; create_server()"`
Expected: no output, exit code 0.

- [ ] **Step 7: Commit**

```bash
git add src/mcp_local_reference/tools/collections.py \
        src/mcp_local_reference/server.py \
        tests/test_collections.py
git commit -m "$(cat <<'EOF'
feat(collections): scaffold tool module and private helpers

Adds tools/collections.py with MAX_ITEMS_PER_CALL, _local_collection_snapshot,
_walk_descendants, _check_cycle. Wires register_tools into server.py.
Adds tests/test_collections.py with shared _FakeApi / _FakeZotero
fakes and TestCheckCycle (3 tests).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: `create_collection` tool

`TestCreateCollection`: 6 tests.

**Files:**
- Modify: `src/mcp_local_reference/tools/collections.py`
- Modify: `tests/test_collections.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collections.py`:

```python
# ======================================================================
# create_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import create_collection_impl  # noqa: E402


class TestCreateCollection:
    def test_dry_run_reports_preview(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi()
        result = json.loads(
            create_collection_impl(api, zotero, "LLMs", parent_key="AIK11111", dry_run=True)
        )
        assert result["status"] == "preview"
        assert result["would_create"]["name"] == "LLMs"
        assert result["would_create"]["parent_key"] == "AIK11111"
        assert api.create_collection_calls == []

    def test_already_exists_short_circuits_dry_run(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI"), _coll("LLMK2222", "LLMs", "AIK11111")]
        )
        result = json.loads(
            create_collection_impl(_FakeApi(), zotero, "LLMs", parent_key="AIK11111", dry_run=True)
        )
        assert result["status"] == "already_exists"
        assert result["existing_key"] == "LLMK2222"

    def test_parent_missing_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(
            create_collection_impl(_FakeApi(), zotero, "LLMs", parent_key="NOPE9999", dry_run=True)
        )
        assert "error" in result
        assert "NOPE9999" in result["error"]

    def test_write_creates_collection(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            created_snapshot=CollectionSnapshot(
                collection_key="LLMK2222",
                version=5,
                name="LLMs",
                parent_key="AIK11111",
                raw={},
            )
        )
        result = json.loads(
            create_collection_impl(api, zotero, "LLMs", parent_key="AIK11111", dry_run=False)
        )
        assert result["status"] == "applied"
        assert result["created"]["collection_key"] == "LLMK2222"
        assert result["new_version"] == 5
        assert api.create_collection_calls == [("LLMs", "AIK11111")]

    def test_write_short_circuits_when_already_exists(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI"), _coll("LLMK2222", "LLMs", "AIK11111")]
        )
        api = _FakeApi()
        result = json.loads(
            create_collection_impl(api, zotero, "LLMs", parent_key="AIK11111", dry_run=False)
        )
        assert result["status"] == "already_exists"
        assert api.create_collection_calls == []  # no POST issued

    def test_write_with_missing_credentials_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[])
        api = _FakeApi(create_collection_error=MissingCredentialsError("no creds"))
        result = json.loads(
            create_collection_impl(api, zotero, "Top", parent_key=None, dry_run=False)
        )
        assert "error" in result
        assert "no creds" in result["error"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_collections.py::TestCreateCollection -v`
Expected: FAIL — `ImportError: cannot import name 'create_collection_impl'`.

- [ ] **Step 3: Implement `create_collection_impl` and the MCP wrapper**

In `src/mcp_local_reference/tools/collections.py`, add the impl below the helpers (and register the MCP tool inside `register_tools`).

```python
# Add inside register_tools, after the placeholder line:
    _register_create_collection(mcp, api, zotero)
```

Then add the registrar function and the impl at module level:

```python
def _register_create_collection(
    mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient
) -> None:
    @mcp.tool()
    def create_collection(
        name: str, parent_key: str | None = None, dry_run: bool = True
    ) -> str:
        """Create a new Zotero collection (folder).

        Defaults to dry_run=True. If a collection with the same (name,
        parent_key) already exists, returns status='already_exists' with
        the existing key — no duplicate is created.

        Args:
            name: The collection name. Must be non-empty.
            parent_key: Parent collection key, or None to create at the
                library root.
            dry_run: If True (default), report the preview without writing.
        """
        return create_collection_impl(api, zotero, name, parent_key, dry_run)


def create_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    name: str,
    parent_key: str | None,
    dry_run: bool,
) -> str:
    name = (name or "").strip()
    if not name:
        return _err("No collection name provided")

    all_cols = _flatten_tree(zotero.list_collections())
    by_key = {c.key: c for c in all_cols}

    if parent_key is not None and parent_key not in by_key:
        return _err(f"Parent collection '{parent_key}' not found")

    existing = next(
        (c for c in all_cols if c.name == name and c.parent_key == parent_key),
        None,
    )
    if existing is not None:
        return json.dumps(
            {
                "status": "already_exists",
                "existing_key": existing.key,
                "name": name,
                "parent_key": parent_key,
                "dry_run": dry_run,
            }
        )

    if dry_run:
        return json.dumps(
            {
                "status": "preview",
                "would_create": {"name": name, "parent_key": parent_key},
                "dry_run": True,
            }
        )

    try:
        snap = api.create_collection(name, parent_key)
    except (MissingCredentialsError, ZoteroApiError) as exc:
        return _err(str(exc))

    return json.dumps(
        {
            "status": "applied",
            "created": {
                "collection_key": snap.collection_key,
                "name": snap.name,
                "parent_key": snap.parent_key,
            },
            "new_version": snap.version,
            "dry_run": False,
        }
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_collections.py::TestCreateCollection -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/tools/collections.py tests/test_collections.py
git commit -m "$(cat <<'EOF'
feat(collections): implement create_collection tool

POST /collections with idempotency check (refuses to create a duplicate
under the same parent). Dry-run reads local SQLite for the existence
check; write delegates to ZoteroApiClient.create_collection.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: `rename_collection` tool

`TestRenameCollection`: 6 tests.

**Files:**
- Modify: `src/mcp_local_reference/tools/collections.py`
- Modify: `tests/test_collections.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collections.py`:

```python
# ======================================================================
# rename_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import rename_collection_impl  # noqa: E402


class TestRenameCollection:
    def test_dry_run_reports_preview(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(
            rename_collection_impl(_FakeApi(), zotero, "AIK11111", "Artificial Intelligence", dry_run=True)
        )
        assert result["status"] == "preview"
        assert result["current"]["name"] == "AI"
        assert result["after"]["name"] == "Artificial Intelligence"

    def test_dry_run_returns_error_when_collection_missing(self) -> None:
        result = json.loads(
            rename_collection_impl(_FakeApi(), _FakeZotero(), "NOPE9999", "X", dry_run=True)
        )
        assert "error" in result

    def test_write_renames(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={
                "AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})
            },
            new_version=8,
        )
        result = json.loads(
            rename_collection_impl(api, zotero, "AIK11111", "Artificial Intelligence", dry_run=False)
        )
        assert result["status"] == "applied"
        assert result["new_version"] == 8
        assert api.update_collection_calls == [
            ("AIK11111", {"name": "Artificial Intelligence"}, 7)
        ]

    def test_write_no_op_when_name_unchanged(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={"AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})}
        )
        result = json.loads(
            rename_collection_impl(api, zotero, "AIK11111", "AI", dry_run=False)
        )
        assert result["status"] == "no_changes"
        assert api.update_collection_calls == []

    def test_write_refuses_sibling_name_collision(self) -> None:
        zotero = _FakeZotero(
            collections=[
                _coll("AIK11111", "AI"),
                _coll("OTRK2222", "ML"),
            ]
        )
        result = json.loads(
            rename_collection_impl(_FakeApi(), zotero, "AIK11111", "ML", dry_run=False)
        )
        assert "error" in result
        assert "ML" in result["error"]

    def test_write_version_conflict_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={"AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})},
            update_collection_error=VersionConflictError("conflict"),
        )
        result = json.loads(
            rename_collection_impl(api, zotero, "AIK11111", "X", dry_run=False)
        )
        assert "error" in result
        assert "conflict" in result["error"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_collections.py::TestRenameCollection -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `rename_collection_impl`**

In `src/mcp_local_reference/tools/collections.py`, add to `register_tools`:

```python
    _register_rename_collection(mcp, api, zotero)
```

And append:

```python
def _register_rename_collection(
    mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient
) -> None:
    @mcp.tool()
    def rename_collection(
        collection_key: str, new_name: str, dry_run: bool = True
    ) -> str:
        """Rename an existing Zotero collection.

        Defaults to dry_run=True. Refuses sibling-name collisions
        (a collection with the same new_name already under the same
        parent) for hygiene; Zotero itself permits this.
        """
        return rename_collection_impl(api, zotero, collection_key, new_name, dry_run)


def rename_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    new_name: str,
    dry_run: bool,
) -> str:
    new_name = (new_name or "").strip()
    if not new_name:
        return _err("No new name provided")

    local = _local_collection_snapshot(zotero, collection_key)
    if local is None:
        return _err(f"Reference '{collection_key}' not found")

    if local.name == new_name:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "status": "no_changes",
                "dry_run": dry_run,
            }
        )

    siblings = [
        c
        for c in _flatten_tree(zotero.list_collections())
        if c.parent_key == local.parent_key and c.key != collection_key
    ]
    if any(c.name == new_name for c in siblings):
        return _err(
            f"A collection named '{new_name}' already exists under this parent"
        )

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "would_rename_to": new_name,
                "after": {"name": new_name, "parent_key": local.parent_key},
                "status": "preview",
                "dry_run": True,
            }
        )

    try:
        snap = api.get_collection(collection_key)
        new_version = api.update_collection(
            collection_key, name=new_name, version=snap.version
        )
    except (MissingCredentialsError, VersionConflictError, ZoteroApiError) as exc:
        return _err(str(exc))

    return json.dumps(
        {
            "collection_key": collection_key,
            "current": {"name": local.name, "parent_key": local.parent_key},
            "after": {"name": new_name, "parent_key": local.parent_key},
            "new_version": new_version,
            "status": "applied",
            "dry_run": False,
        }
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_collections.py::TestRenameCollection -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/tools/collections.py tests/test_collections.py
git commit -m "$(cat <<'EOF'
feat(collections): implement rename_collection tool

PATCH /collections/<key> with name only. Refuses sibling-name
collisions for hygiene. No-op short-circuit when new name equals
current name. Dry-run reads local SQLite; write fetches authoritative
version from the Web API.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: `reparent_collection` tool

`TestReparentCollection`: 7 tests (cycle-to-self and cycle-to-descendant are separate tests).

**Files:**
- Modify: `src/mcp_local_reference/tools/collections.py`
- Modify: `tests/test_collections.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collections.py`:

```python
# ======================================================================
# reparent_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import reparent_collection_impl  # noqa: E402


class TestReparentCollection:
    def test_dry_run_reports_preview(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI"), _coll("OTRK2222", "Other")]
        )
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "OTRK2222", dry_run=True)
        )
        assert result["status"] == "preview"
        assert result["after"]["parent_key"] == "OTRK2222"

    def test_dry_run_to_root(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI", "OTRK2222"), _coll("OTRK2222", "Other")]
        )
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", None, dry_run=True)
        )
        assert result["status"] == "preview"
        assert result["after"]["parent_key"] is None

    def test_dry_run_no_op_when_parent_unchanged(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI", "OTRK2222"), _coll("OTRK2222", "Other")]
        )
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "OTRK2222", dry_run=True)
        )
        assert result["status"] == "no_changes"

    def test_parent_missing_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "NOPE9999", dry_run=True)
        )
        assert "error" in result

    def test_cycle_to_self_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "AIK11111", dry_run=True)
        )
        assert "error" in result
        assert "Cycle" in result["error"]

    def test_cycle_to_descendant_returns_error(self) -> None:
        zotero = _FakeZotero(
            collections=[
                _coll("AIK11111", "AI"),
                _coll("LLMK2222", "LLMs", "AIK11111"),
            ]
        )
        result = json.loads(
            reparent_collection_impl(_FakeApi(), zotero, "AIK11111", "LLMK2222", dry_run=True)
        )
        assert "error" in result
        assert "Cycle" in result["error"]

    def test_write_reparents(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI"), _coll("OTRK2222", "Other")]
        )
        api = _FakeApi(
            collection_snapshots={
                "AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})
            },
            new_version=8,
        )
        result = json.loads(
            reparent_collection_impl(api, zotero, "AIK11111", "OTRK2222", dry_run=False)
        )
        assert result["status"] == "applied"
        assert result["new_version"] == 8
        assert api.update_collection_calls == [
            ("AIK11111", {"parent_key": "OTRK2222"}, 7)
        ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_collections.py::TestReparentCollection -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `reparent_collection_impl`**

In `src/mcp_local_reference/tools/collections.py`, add to `register_tools`:

```python
    _register_reparent_collection(mcp, api, zotero)
```

And append:

```python
def _register_reparent_collection(
    mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient
) -> None:
    @mcp.tool()
    def reparent_collection(
        collection_key: str,
        new_parent_key: str | None,
        dry_run: bool = True,
    ) -> str:
        """Move a collection under a different parent.

        Pass new_parent_key=None to move to the library root.
        Refuses cycles (re-parenting a collection under itself or one
        of its descendants).
        """
        return reparent_collection_impl(api, zotero, collection_key, new_parent_key, dry_run)


def reparent_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    new_parent_key: str | None,
    dry_run: bool,
) -> str:
    local = _local_collection_snapshot(zotero, collection_key)
    if local is None:
        return _err(f"Reference '{collection_key}' not found")

    if new_parent_key is not None:
        all_keys = {c.key for c in _flatten_tree(zotero.list_collections())}
        if new_parent_key not in all_keys:
            return _err(f"Parent collection '{new_parent_key}' not found")
        if _check_cycle(zotero, collection_key, new_parent_key):
            return _err(
                f"Cycle detected: collection {collection_key} cannot be re-parented "
                f"under its own descendant {new_parent_key}"
            )

    if local.parent_key == new_parent_key:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "status": "no_changes",
                "dry_run": dry_run,
            }
        )

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "would_reparent_to": new_parent_key,
                "after": {"name": local.name, "parent_key": new_parent_key},
                "status": "preview",
                "dry_run": True,
            }
        )

    try:
        snap = api.get_collection(collection_key)
        new_version = api.update_collection(
            collection_key, parent_key=new_parent_key, version=snap.version
        )
    except (MissingCredentialsError, VersionConflictError, ZoteroApiError) as exc:
        return _err(str(exc))

    return json.dumps(
        {
            "collection_key": collection_key,
            "current": {"name": local.name, "parent_key": local.parent_key},
            "after": {"name": local.name, "parent_key": new_parent_key},
            "new_version": new_version,
            "status": "applied",
            "dry_run": False,
        }
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_collections.py::TestReparentCollection -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/tools/collections.py tests/test_collections.py
git commit -m "$(cat <<'EOF'
feat(collections): implement reparent_collection tool

PATCH /collections/<key> with parent_key only. Detects cycles locally
(target = new parent OR new parent is a descendant of target) before
any API call. None means library root.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: `delete_collection` tool

`TestDeleteCollection`: 6 tests.

**Files:**
- Modify: `src/mcp_local_reference/tools/collections.py`
- Modify: `tests/test_collections.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collections.py`:

```python
# ======================================================================
# delete_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import delete_collection_impl  # noqa: E402


class TestDeleteCollection:
    def test_dry_run_empty_collection(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        result = json.loads(
            delete_collection_impl(_FakeApi(), zotero, "AIK11111", dry_run=True)
        )
        assert result["status"] == "preview"
        assert result["would_orphan_items"] == []
        assert result["would_orphan_collections"] == []

    def test_dry_run_with_items_lists_them(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA"), "ITEMB": _ref("ITEMB")},
            items_per_collection={"AIK11111": ["ITEMA", "ITEMB"]},
        )
        result = json.loads(
            delete_collection_impl(_FakeApi(), zotero, "AIK11111", dry_run=True)
        )
        assert sorted(result["would_orphan_items"]) == ["ITEMA", "ITEMB"]

    def test_dry_run_with_child_collections_lists_them(self) -> None:
        zotero = _FakeZotero(
            collections=[
                _coll("AIK11111", "AI"),
                _coll("LLMK2222", "LLMs", "AIK11111"),
                _coll("MMK33333", "Multimodal", "AIK11111"),
            ]
        )
        result = json.loads(
            delete_collection_impl(_FakeApi(), zotero, "AIK11111", dry_run=True)
        )
        assert sorted(result["would_orphan_collections"]) == ["LLMK2222", "MMK33333"]

    def test_dry_run_returns_error_when_missing(self) -> None:
        result = json.loads(
            delete_collection_impl(_FakeApi(), _FakeZotero(), "NOPE9999", dry_run=True)
        )
        assert "error" in result

    def test_write_deletes(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={
                "AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})
            }
        )
        result = json.loads(
            delete_collection_impl(api, zotero, "AIK11111", dry_run=False)
        )
        assert result["status"] == "applied"
        assert api.delete_collection_calls == [("AIK11111", 7)]

    def test_write_version_conflict_returns_error(self) -> None:
        zotero = _FakeZotero(collections=[_coll("AIK11111", "AI")])
        api = _FakeApi(
            collection_snapshots={
                "AIK11111": CollectionSnapshot("AIK11111", 7, "AI", None, {})
            },
            delete_collection_error=VersionConflictError("conflict"),
        )
        result = json.loads(
            delete_collection_impl(api, zotero, "AIK11111", dry_run=False)
        )
        assert "error" in result
        assert "conflict" in result["error"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_collections.py::TestDeleteCollection -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `delete_collection_impl`**

In `src/mcp_local_reference/tools/collections.py`, add to `register_tools`:

```python
    _register_delete_collection(mcp, api, zotero)
```

And append:

```python
def _register_delete_collection(
    mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient
) -> None:
    @mcp.tool()
    def delete_collection(collection_key: str, dry_run: bool = True) -> str:
        """Delete a collection. Items inside are NOT deleted — they lose
        membership in this collection. Sub-collections become orphans.
        Dry-run lists both blast surfaces before any write.
        """
        return delete_collection_impl(api, zotero, collection_key, dry_run)


def delete_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    dry_run: bool,
) -> str:
    local = _local_collection_snapshot(zotero, collection_key)
    if local is None:
        return _err(f"Reference '{collection_key}' not found")

    items_inside = [r.item_key for r in zotero.get_collection_items(collection_key, limit=10_000)]
    descendants = sorted(_walk_descendants(zotero, collection_key))

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "current": {"name": local.name, "parent_key": local.parent_key},
                "would_orphan_items": items_inside,
                "would_orphan_collections": descendants,
                "status": "preview",
                "dry_run": True,
            }
        )

    try:
        snap = api.get_collection(collection_key)
        api.delete_collection(collection_key, snap.version)
    except (MissingCredentialsError, VersionConflictError, ZoteroApiError) as exc:
        return _err(str(exc))

    return json.dumps(
        {
            "collection_key": collection_key,
            "deleted": {"name": local.name, "parent_key": local.parent_key},
            "would_orphan_items": items_inside,
            "would_orphan_collections": descendants,
            "status": "applied",
            "dry_run": False,
        }
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_collections.py::TestDeleteCollection -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/tools/collections.py tests/test_collections.py
git commit -m "$(cat <<'EOF'
feat(collections): implement delete_collection tool

DELETE /collections/<key>. Dry-run enumerates items losing membership
and child collections that will be orphaned. No extra force flag —
dry_run is the safety gate (Q7: A in the spec).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: `add_items_to_collection` tool

`TestAddItemsToCollection`: 7 tests. Introduces partial-failure semantics.

**Files:**
- Modify: `src/mcp_local_reference/tools/collections.py`
- Modify: `tests/test_collections.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collections.py`:

```python
# ======================================================================
# add_items_to_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import (  # noqa: E402
    add_items_to_collection_impl,
)


class TestAddItemsToCollection:
    def test_rejects_empty_list(self) -> None:
        result = json.loads(
            add_items_to_collection_impl(_FakeApi(), _FakeZotero(), "AIK11111", [], dry_run=True)
        )
        assert "error" in result

    def test_rejects_too_many_items(self) -> None:
        too_many = [f"K{i:07d}" for i in range(MAX_ITEMS_PER_CALL + 1)]
        result = json.loads(
            add_items_to_collection_impl(_FakeApi(), _FakeZotero(), "AIK11111", too_many, dry_run=True)
        )
        assert "error" in result
        assert "exceeds" in result["error"]

    def test_dry_run_partitions_correctly(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA"), "ITEMB": _ref("ITEMB")},
            item_collections={"ITEMA": [], "ITEMB": ["AIK11111"]},
        )
        result = json.loads(
            add_items_to_collection_impl(
                _FakeApi(), zotero, "AIK11111", ["ITEMA", "ITEMB", "MISSING0"], dry_run=True
            )
        )
        assert result["status"] == "preview"
        assert result["would_add"] == ["ITEMA"]
        assert result["already_present"] == ["ITEMB"]
        assert result["not_found"] == ["MISSING0"]

    def test_collection_missing_returns_error(self) -> None:
        result = json.loads(
            add_items_to_collection_impl(_FakeApi(), _FakeZotero(), "NOPE9999", ["ITEMA"], dry_run=True)
        )
        assert "error" in result

    def test_write_succeeds_clean(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA")},
            item_collections={"ITEMA": []},
        )
        api = _FakeApi(
            item_snapshots={
                "ITEMA": ItemSnapshot(
                    item_key="ITEMA", version=10, tags=[], collections=[], raw={}
                )
            },
            new_version=11,
        )
        result = json.loads(
            add_items_to_collection_impl(api, zotero, "AIK11111", ["ITEMA"], dry_run=False)
        )
        assert result["status"] == "applied"
        assert result["succeeded"] == [{"item_key": "ITEMA", "new_version": 11}]
        assert result["failed"] == []
        assert api.update_item_calls == [("ITEMA", ["AIK11111"], 10)]

    def test_write_records_partial_failure(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA"), "ITEMB": _ref("ITEMB")},
            item_collections={"ITEMA": [], "ITEMB": []},
        )
        # First item ok; second raises VersionConflict.
        snapshots = {
            "ITEMA": ItemSnapshot("ITEMA", 10, [], [], {}),
            "ITEMB": ItemSnapshot("ITEMB", 20, [], [], {}),
        }

        class _PartialApi(_FakeApi):
            def update_item_collections(self, item_key, collection_keys, version):
                self.update_item_calls.append((item_key, list(collection_keys), version))
                if item_key == "ITEMB":
                    raise VersionConflictError("conflict on ITEMB")
                return self.new_version

        api = _PartialApi(item_snapshots=snapshots, new_version=11)
        result = json.loads(
            add_items_to_collection_impl(api, zotero, "AIK11111", ["ITEMA", "ITEMB"], dry_run=False)
        )
        assert result["status"] == "partial"
        assert result["succeeded"] == [{"item_key": "ITEMA", "new_version": 11}]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["item_key"] == "ITEMB"

    def test_write_short_circuits_when_all_already_present(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA")},
            item_collections={"ITEMA": ["AIK11111"]},
        )
        api = _FakeApi()
        result = json.loads(
            add_items_to_collection_impl(api, zotero, "AIK11111", ["ITEMA"], dry_run=False)
        )
        assert result["status"] == "no_changes"
        assert api.update_item_calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_collections.py::TestAddItemsToCollection -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `add_items_to_collection_impl`**

In `src/mcp_local_reference/tools/collections.py`, add to `register_tools`:

```python
    _register_add_items_to_collection(mcp, api, zotero)
```

And append:

```python
def _register_add_items_to_collection(
    mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient
) -> None:
    @mcp.tool()
    def add_items_to_collection(
        collection_key: str,
        item_keys: list[str],
        dry_run: bool = True,
    ) -> str:
        """Add items to a Zotero collection (set-union per item).

        Defaults to dry_run=True. Per-call cap of 25 items. Each item
        is patched independently — partial failures are reported in the
        `failed` list while successful items are committed.
        """
        return add_items_to_collection_impl(
            api, zotero, collection_key, item_keys, dry_run
        )


def add_items_to_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    item_keys: list[str],
    dry_run: bool,
) -> str:
    cleaned = [k.strip() for k in item_keys if k and k.strip()]
    if not cleaned:
        return _err("No item keys provided")
    if len(cleaned) > MAX_ITEMS_PER_CALL:
        return _err(
            f"Refusing: {len(cleaned)} items exceeds the per-call cap of "
            f"{MAX_ITEMS_PER_CALL}. Split into smaller batches."
        )

    coll_snap = _local_collection_snapshot(zotero, collection_key)
    if coll_snap is None:
        return _err(f"Reference '{collection_key}' not found")

    would_add: list[str] = []
    already_present: list[str] = []
    not_found: list[str] = []
    for k in cleaned:
        if zotero.get_reference(k) is None:
            not_found.append(k)
            continue
        current = zotero.get_item_collections(k)
        if collection_key in current:
            already_present.append(k)
        else:
            would_add.append(k)

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "would_add": would_add,
                "already_present": already_present,
                "not_found": not_found,
                "status": "preview",
                "dry_run": True,
            }
        )

    if not would_add:
        return json.dumps(
            {
                "collection_key": collection_key,
                "succeeded": [],
                "failed": [],
                "already_present": already_present,
                "not_found": not_found,
                "status": "no_changes",
                "dry_run": False,
            }
        )

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item_key in would_add:
        try:
            snap = api.get_item(item_key)
            new_colls = sorted({*snap.collections, collection_key})
            new_version = api.update_item_collections(item_key, new_colls, snap.version)
            succeeded.append({"item_key": item_key, "new_version": new_version})
        except MissingCredentialsError as exc:
            return _err(str(exc))
        except (VersionConflictError, ZoteroApiError) as exc:
            failed.append({"item_key": item_key, "reason": str(exc)})

    return json.dumps(
        {
            "collection_key": collection_key,
            "succeeded": succeeded,
            "failed": failed,
            "already_present": already_present,
            "not_found": not_found,
            "status": "partial" if failed else "applied",
            "dry_run": False,
        }
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_collections.py::TestAddItemsToCollection -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/tools/collections.py tests/test_collections.py
git commit -m "$(cat <<'EOF'
feat(collections): implement add_items_to_collection tool

PATCH /items/<key> per item with set-union of collections. Cap of 25,
dry-run preview, partial-failure semantics: items with version
conflicts go to `failed[]`, the rest still apply.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: `remove_items_from_collection` tool

`TestRemoveItemsFromCollection`: 5 tests. Mirror of Task 13 with set-difference.

**Files:**
- Modify: `src/mcp_local_reference/tools/collections.py`
- Modify: `tests/test_collections.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collections.py`:

```python
# ======================================================================
# remove_items_from_collection_impl
# ======================================================================


from mcp_local_reference.tools.collections import (  # noqa: E402
    remove_items_from_collection_impl,
)


class TestRemoveItemsFromCollection:
    def test_rejects_empty_list(self) -> None:
        result = json.loads(
            remove_items_from_collection_impl(
                _FakeApi(), _FakeZotero(), "AIK11111", [], dry_run=True
            )
        )
        assert "error" in result

    def test_rejects_too_many_items(self) -> None:
        too_many = [f"K{i:07d}" for i in range(MAX_ITEMS_PER_CALL + 1)]
        result = json.loads(
            remove_items_from_collection_impl(
                _FakeApi(), _FakeZotero(), "AIK11111", too_many, dry_run=True
            )
        )
        assert "error" in result

    def test_dry_run_partitions(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA"), "ITEMB": _ref("ITEMB")},
            item_collections={"ITEMA": ["AIK11111"], "ITEMB": []},
        )
        result = json.loads(
            remove_items_from_collection_impl(
                _FakeApi(), zotero, "AIK11111", ["ITEMA", "ITEMB"], dry_run=True
            )
        )
        assert result["status"] == "preview"
        assert result["would_remove"] == ["ITEMA"]
        assert result["not_present"] == ["ITEMB"]

    def test_write_succeeds_clean(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA")},
            item_collections={"ITEMA": ["AIK11111", "OTHKEY99"]},
        )
        api = _FakeApi(
            item_snapshots={
                "ITEMA": ItemSnapshot(
                    "ITEMA", 10, [], ["AIK11111", "OTHKEY99"], {}
                )
            },
            new_version=11,
        )
        result = json.loads(
            remove_items_from_collection_impl(
                api, zotero, "AIK11111", ["ITEMA"], dry_run=False
            )
        )
        assert result["status"] == "applied"
        assert api.update_item_calls == [("ITEMA", ["OTHKEY99"], 10)]

    def test_write_short_circuits_when_all_not_present(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA")},
            item_collections={"ITEMA": []},
        )
        api = _FakeApi()
        result = json.loads(
            remove_items_from_collection_impl(
                api, zotero, "AIK11111", ["ITEMA"], dry_run=False
            )
        )
        assert result["status"] == "no_changes"
        assert api.update_item_calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_collections.py::TestRemoveItemsFromCollection -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `remove_items_from_collection_impl`**

In `src/mcp_local_reference/tools/collections.py`, add to `register_tools`:

```python
    _register_remove_items_from_collection(mcp, api, zotero)
```

And append:

```python
def _register_remove_items_from_collection(
    mcp: FastMCP, api: ZoteroApiClient, zotero: ZoteroClient
) -> None:
    @mcp.tool()
    def remove_items_from_collection(
        collection_key: str,
        item_keys: list[str],
        dry_run: bool = True,
    ) -> str:
        """Remove items from a collection (set-difference per item).

        Mirror of add_items_to_collection. Items not currently in the
        collection are reported under `not_present` and silently skipped.
        Cap of 25, partial-failure semantics.
        """
        return remove_items_from_collection_impl(
            api, zotero, collection_key, item_keys, dry_run
        )


def remove_items_from_collection_impl(
    api: ZoteroApiClient,
    zotero: ZoteroClient,
    collection_key: str,
    item_keys: list[str],
    dry_run: bool,
) -> str:
    cleaned = [k.strip() for k in item_keys if k and k.strip()]
    if not cleaned:
        return _err("No item keys provided")
    if len(cleaned) > MAX_ITEMS_PER_CALL:
        return _err(
            f"Refusing: {len(cleaned)} items exceeds the per-call cap of "
            f"{MAX_ITEMS_PER_CALL}. Split into smaller batches."
        )

    coll_snap = _local_collection_snapshot(zotero, collection_key)
    if coll_snap is None:
        return _err(f"Reference '{collection_key}' not found")

    would_remove: list[str] = []
    not_present: list[str] = []
    not_found: list[str] = []
    for k in cleaned:
        if zotero.get_reference(k) is None:
            not_found.append(k)
            continue
        current = zotero.get_item_collections(k)
        if collection_key in current:
            would_remove.append(k)
        else:
            not_present.append(k)

    if dry_run:
        return json.dumps(
            {
                "collection_key": collection_key,
                "would_remove": would_remove,
                "not_present": not_present,
                "not_found": not_found,
                "status": "preview",
                "dry_run": True,
            }
        )

    if not would_remove:
        return json.dumps(
            {
                "collection_key": collection_key,
                "succeeded": [],
                "failed": [],
                "not_present": not_present,
                "not_found": not_found,
                "status": "no_changes",
                "dry_run": False,
            }
        )

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item_key in would_remove:
        try:
            snap = api.get_item(item_key)
            new_colls = [c for c in snap.collections if c != collection_key]
            new_version = api.update_item_collections(item_key, new_colls, snap.version)
            succeeded.append({"item_key": item_key, "new_version": new_version})
        except MissingCredentialsError as exc:
            return _err(str(exc))
        except (VersionConflictError, ZoteroApiError) as exc:
            failed.append({"item_key": item_key, "reason": str(exc)})

    return json.dumps(
        {
            "collection_key": collection_key,
            "succeeded": succeeded,
            "failed": failed,
            "not_present": not_present,
            "not_found": not_found,
            "status": "partial" if failed else "applied",
            "dry_run": False,
        }
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_collections.py::TestRemoveItemsFromCollection -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_local_reference/tools/collections.py tests/test_collections.py
git commit -m "$(cat <<'EOF'
feat(collections): implement remove_items_from_collection tool

PATCH /items/<key> per item with set-difference of collections. Mirror
of add_items_to_collection — same cap, dry-run, and partial-failure
semantics, with would_remove / not_present partition.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: `suggest_collection_placement` read tool

`TestSuggestCollectionPlacement`: 3 tests. Read-only — gathers context for Claude.

**Files:**
- Modify: `src/mcp_local_reference/tools/collections.py`
- Modify: `tests/test_collections.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_collections.py`:

```python
# ======================================================================
# suggest_collection_placement_impl
# ======================================================================


from mcp_local_reference.tools.collections import (  # noqa: E402
    suggest_collection_placement_impl,
)


class _NoOpPdf:
    """Stand-in for PdfProcessor — placement suggester only needs first-page text."""

    def first_page_text(self, path) -> str:  # pragma: no cover (stubbed)
        return ""


class TestSuggestCollectionPlacement:
    def test_returns_title_abstract_tree_and_counts(self) -> None:
        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI"), _coll("LLMK2222", "LLMs", "AIK11111")],
            references={
                "ITEMA": _ref("ITEMA", title="Title A", abstract="Abstract about LLMs.")
            },
            item_collections={"ITEMA": ["AIK11111"]},
            items_per_collection={"AIK11111": ["ITEMA"], "LLMK2222": []},
        )
        result = json.loads(
            suggest_collection_placement_impl(zotero, _NoOpPdf(), "ITEMA")
        )
        assert result["item"]["title"] == "Title A"
        assert "LLMs" in result["item"]["abstract_or_snippet"]
        assert result["item"]["current_collections"] == [
            {"key": "AIK11111", "name": "AI"}
        ]
        assert any(c["key"] == "AIK11111" for c in result["collection_tree"])
        assert result["vocabulary"]["AIK11111"] == 1
        assert result["vocabulary"]["LLMK2222"] == 0

    def test_falls_back_to_pdf_snippet_when_abstract_empty(self) -> None:
        class _Pdf:
            def first_page_text(self, path) -> str:
                return "First page snippet text."

        zotero = _FakeZotero(
            collections=[_coll("AIK11111", "AI")],
            references={"ITEMA": _ref("ITEMA", title="T", abstract="")},
            item_collections={"ITEMA": []},
            items_per_collection={"AIK11111": []},
        )
        # The impl needs a PDF path lookup — stub get_pdf_path on the fake.
        zotero.get_pdf_path = lambda k: "/tmp/fake.pdf"  # type: ignore[attr-defined]

        result = json.loads(
            suggest_collection_placement_impl(zotero, _Pdf(), "ITEMA")
        )
        assert "First page snippet text." in result["item"]["abstract_or_snippet"]

    def test_returns_error_when_item_missing(self) -> None:
        result = json.loads(
            suggest_collection_placement_impl(_FakeZotero(), _NoOpPdf(), "NOPE9999")
        )
        assert "error" in result
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_collections.py::TestSuggestCollectionPlacement -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `suggest_collection_placement_impl`**

In `src/mcp_local_reference/tools/collections.py`, add to `register_tools`:

```python
    _register_suggest_collection_placement(mcp, zotero, pdf)
```

And append:

```python
def _register_suggest_collection_placement(
    mcp: FastMCP, zotero: ZoteroClient, pdf: PdfProcessor
) -> None:
    @mcp.tool()
    def suggest_collection_placement(item_key: str) -> str:
        """Gather context for advising where an item should be filed.

        Returns the item's title and abstract (or first-page PDF snippet
        when the abstract is empty), the full collection tree, the
        item's current memberships, and a per-collection item count so
        well-populated folders can be preferred over thinly-used ones.
        Read-only — no writes.
        """
        return suggest_collection_placement_impl(zotero, pdf, item_key)


def suggest_collection_placement_impl(
    zotero: ZoteroClient,
    pdf: PdfProcessor,
    item_key: str,
) -> str:
    ref = zotero.get_reference(item_key)
    if ref is None:
        return _err(f"Reference '{item_key}' not found")

    abstract = (ref.abstract or "").strip()
    if not abstract:
        try:
            pdf_path = zotero.get_pdf_path(item_key)
        except Exception:  # noqa: BLE001 — local helper, never crash the tool
            pdf_path = None
        if pdf_path is not None:
            try:
                snippet = pdf.first_page_text(pdf_path)
                if snippet:
                    abstract = snippet[:2000]
            except Exception:  # noqa: BLE001
                pass

    all_cols = _flatten_tree(zotero.list_collections())
    by_key = {c.key: c for c in all_cols}
    current_keys = zotero.get_item_collections(item_key)
    current_collections = [
        {"key": k, "name": by_key[k].name} for k in current_keys if k in by_key
    ]

    vocabulary: dict[str, int] = {}
    for c in all_cols:
        # 10_000 is a high upper bound to avoid silent truncation; cap is local-side.
        items = zotero.get_collection_items(c.key, limit=10_000)
        vocabulary[c.key] = len(items)

    return json.dumps(
        {
            "item": {
                "item_key": item_key,
                "title": ref.title,
                "abstract_or_snippet": abstract,
                "current_collections": current_collections,
            },
            "collection_tree": [_collection_to_dict(c) for c in zotero.list_collections()],
            "vocabulary": vocabulary,
        }
    )


def _collection_to_dict(col: Collection) -> dict[str, Any]:
    return {
        "key": col.key,
        "name": col.name,
        "parent_key": col.parent_key,
        "children": [_collection_to_dict(c) for c in col.children],
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_collections.py::TestSuggestCollectionPlacement -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the full suite + ruff**

Run: `uv run pytest -v && uv run ruff check src/ tests/ && uv run ruff format src/ tests/ --check`
Expected: PASS — 196 tests; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_local_reference/tools/collections.py tests/test_collections.py
git commit -m "$(cat <<'EOF'
feat(collections): implement suggest_collection_placement read tool

Read-only tool gathering item title + abstract (PDF snippet fallback) +
collection tree + per-collection item counts. Vocabulary anchoring lets
Claude prefer well-populated folders over thinly-used ones when the
user asks for placement advice.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 16: Update CLAUDE.md

Add a "Collection editing" paragraph at the same level of detail as the existing auto-tagging paragraph.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Edit the auto-tagging paragraph**

In `CLAUDE.md`, find the bullet starting `**Auto-tagging (`tools/auto_tag.py`):**`. Insert a new sibling bullet immediately after it:

```markdown
- **Collection editing (`tools/collections.py`):** seven MCP tools mirroring the auto-tagging discipline — six write tools (`create_collection`, `rename_collection`, `reparent_collection`, `delete_collection`, `add_items_to_collection`, `remove_items_from_collection`) and one read tool (`suggest_collection_placement`). All write tools default to `dry_run=True` with optimistic concurrency via `If-Unmodified-Since-Version`; item-membership tools cap input at 25 items per call (`MAX_ITEMS_PER_CALL`). `delete_collection` previews items and child collections that would be orphaned (Zotero's API doesn't cascade-delete). `reparent_collection` detects cycles locally before any API call. `suggest_collection_placement` is read-only and returns the item's title + abstract (PDF snippet fallback), the full collection tree, and per-collection item counts so Claude can advise placement without writing.
```

- [ ] **Step 2: Run a final smoke check**

Run: `uv run pytest -v && uv run ruff check src/ tests/`
Expected: PASS — 196 tests; ruff clean.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: describe collection-editing tools in CLAUDE.md

One paragraph parallel to the auto-tagging paragraph: lists all 7
tools, calls out the dry-run / 25-item cap / optimistic-concurrency
contract, and notes the cycle-detection and orphan-preview behaviors.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final Verification

After Task 16, run the full check sequence one more time before declaring the feature complete:

```bash
uv run pytest -v
uv run ruff check src/ tests/
uv run ruff format src/ tests/ --check
```

Expected:
- 196 tests pass (150 prior + 46 new).
- ruff check: clean.
- ruff format check: clean.

## Smoke Test (post-suite-green)

Per the spec's success criterion 3: run a real-API smoke test against the live Zotero library. The exact form depends on whether the apply_tags smoke script template lives in the repo. The minimal sequence:

```python
# Run interactively against the real ZoteroApiClient (creds from .env).
from mcp_local_reference.config import Config
from mcp_local_reference.services.zotero_api_client import ZoteroApiClient

c = ZoteroApiClient(Config())
created = c.create_collection("__claude_smoke_test__", parent_key=None)
new_v = c.update_collection(created.collection_key, name="__claude_smoke_renamed__", version=created.version)
# Move under an existing test parent, then back to root.
new_v = c.update_collection(created.collection_key, parent_key="<EXISTING_TEST_PARENT>", version=new_v)
new_v = c.update_collection(created.collection_key, parent_key=None, version=new_v)
# Add an item, then remove it.
item_snap = c.get_item("<SOME_TEST_ITEM_KEY>")
c.update_item_collections(item_snap.item_key, [*item_snap.collections, created.collection_key], item_snap.version)
item_snap = c.get_item(item_snap.item_key)  # refetch for new version
c.update_item_collections(item_snap.item_key, [k for k in item_snap.collections if k != created.collection_key], item_snap.version)
# Delete.
coll = c.get_collection(created.collection_key)
c.delete_collection(coll.collection_key, coll.version)
```

Confirm via the Zotero desktop app (after sync) that the test collection no longer exists and the test item's memberships are unchanged from before the smoke run.

## Branch Finishing

Once all tests pass and the smoke test confirms real-API behavior, follow the existing project convention (CLAUDE.md): fast-forward `main` from `feat/collection-editing`. The brainstorming-to-merge flow is `brainstorming → writing-plans → subagent-driven-development on a feat/* branch, fast-forward merged back to main`. Use `superpowers:finishing-a-development-branch` if helpful.
