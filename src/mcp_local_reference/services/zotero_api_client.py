"""HTTP client for Zotero's Web API (write-path companion to ZoteroClient).

While ``ZoteroClient`` opens the local SQLite database read-only for fast
queries, this client talks to ``api.zotero.org`` for writes. Tags written
here propagate to the local SQLite on the next sync — so the local
``?mode=ro`` invariant is preserved end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from mcp_local_reference.services.resolvers import ZoteroItemDraft

from mcp_local_reference.config import Config


class ZoteroApiError(RuntimeError):
    """Raised when a Zotero Web API call fails."""


class MissingCredentialsError(ZoteroApiError):
    """Raised when ZOTERO_USER_ID or ZOTERO_API_KEY is not set."""


class VersionConflictError(ZoteroApiError):
    """Raised when a write fails because the item changed under us (HTTP 412)."""


@dataclass
class ItemSnapshot:
    """Minimal view of a Zotero item — what we need for tag merging."""

    item_key: str
    version: int
    tags: list[str]
    collections: list[str]
    raw: dict[str, Any]


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


class ZoteroApiClient:
    """Thin httpx-based wrapper for the Zotero Web API."""

    def __init__(
        self,
        config: Config,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self._timeout = timeout
        self._transport = transport

    def _client(self, headers: dict[str, str]) -> httpx.Client:
        return httpx.Client(timeout=self._timeout, headers=headers, transport=self._transport)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_item(self, item_key: str) -> ItemSnapshot:
        """Fetch a single item; needed before any write to capture its version."""
        self._require_credentials()
        url = self._item_url(item_key)
        with self._client(self._headers()) as client:
            response = client.get(url)
        if response.status_code == 404:
            raise ZoteroApiError(f"Item '{item_key}' not found in Zotero Web API")
        response.raise_for_status()
        body = response.json()
        data = body.get("data", body)
        return ItemSnapshot(
            item_key=item_key,
            version=int(data.get("version", 0)),
            tags=[t["tag"] for t in data.get("tags", []) if "tag" in t],
            collections=list(data.get("collections", [])),
            raw=data,
        )

    def set_tags(self, item_key: str, tags: list[str], version: int) -> int:
        """PATCH the item's tag list; returns the new item version.

        Uses ``If-Unmodified-Since-Version`` for optimistic concurrency:
        if the item was modified since *version*, the API returns HTTP 412
        and we raise rather than overwriting the user's other changes.
        """
        self._require_credentials()
        url = self._item_url(item_key)
        headers = {**self._headers(), "If-Unmodified-Since-Version": str(version)}
        body = {"tags": [{"tag": t} for t in tags]}
        with self._client(headers) as client:
            response = client.patch(url, json=body)
        if response.status_code == 412:
            raise VersionConflictError(
                f"Item '{item_key}' was modified since version {version}; refetch and retry"
            )
        if response.status_code == 404:
            raise ZoteroApiError(f"Item '{item_key}' not found")
        response.raise_for_status()
        new_version = response.headers.get("Last-Modified-Version")
        return int(new_version) if new_version else version + 1

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
                f"Item '{item_key}' was modified since version {version}; refetch and retry"
            )
        if response.status_code == 404:
            raise ZoteroApiError(f"Item '{item_key}' not found")
        response.raise_for_status()
        new_version = response.headers.get("Last-Modified-Version")
        return int(new_version) if new_version else version + 1

    def create_item(
        self,
        draft: ZoteroItemDraft,
        collection_key: str | None = None,
    ) -> ItemSnapshot:
        """POST a new item to Zotero. Returns the snapshot of the created item.

        The ``collection_key`` argument, if provided, is included in the POST
        body's ``collections`` array — this avoids a follow-up PATCH and keeps
        the create+file operation atomic from the API's point of view.

        Raises:
            MissingCredentialsError: if user_id / api_key not configured.
            ZoteroApiError: if Zotero rejects the create (``failed`` non-empty).
        """
        self._require_credentials()

        payload: dict[str, Any] = {
            "itemType": draft.item_type,
            **draft.fields,
            "creators": list(draft.creators),
        }
        if collection_key is not None:
            payload["collections"] = [collection_key]

        url = self._items_url()
        with self._client(self._headers()) as client:
            response = client.post(url, json=[payload])
        response.raise_for_status()
        body = response.json()
        if body.get("failed"):
            raise ZoteroApiError(f"Zotero rejected create_item: {body['failed']}")
        try:
            entry = body["successful"]["0"]
        except (KeyError, TypeError) as exc:
            raise ZoteroApiError(f"Unexpected create_item response: {body!r}") from exc

        data = entry["data"]
        return ItemSnapshot(
            item_key=data["key"],
            version=int(data.get("version", 0)),
            tags=[t["tag"] for t in data.get("tags", []) if "tag" in t],
            collections=list(data.get("collections", [])),
            raw=data,
        )

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
            raise ZoteroApiError(f"Collection '{collection_key}' not found in Zotero Web API")
        response.raise_for_status()
        body = response.json()
        data = body.get("data", body)
        return self._collection_snapshot_from_data(data)

    def create_collection(self, name: str, parent_key: str | None) -> CollectionSnapshot:
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
            raise ZoteroApiError(f"Zotero rejected create_collection: {body['failed']}")
        try:
            entry = body["successful"]["0"]
        except (KeyError, TypeError) as exc:
            raise ZoteroApiError(f"Unexpected create_collection response: {body!r}") from exc
        return self._collection_snapshot_from_data(entry["data"])

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

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_credentials(self) -> None:
        if not self.config.zotero_user_id or not self.config.zotero_api_key:
            raise MissingCredentialsError(
                "ZOTERO_USER_ID and ZOTERO_API_KEY must be set to write to Zotero. "
                "Create an API key with library write access at "
                "https://www.zotero.org/settings/keys"
            )

    def _items_url(self) -> str:
        base = self.config.zotero_api_base_url.rstrip("/")
        return f"{base}/users/{self.config.zotero_user_id}/items"

    def _item_url(self, item_key: str) -> str:
        return f"{self._items_url()}/{item_key}"

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

    def _headers(self) -> dict[str, str]:
        return {
            "Zotero-API-Key": self.config.zotero_api_key,
            "Zotero-API-Version": "3",
            "Accept": "application/json",
        }
