"""HTTP client for Zotero's Web API (write-path companion to ZoteroClient).

While ``ZoteroClient`` opens the local SQLite database read-only for fast
queries, this client talks to ``api.zotero.org`` for writes. Tags written
here propagate to the local SQLite on the next sync — so the local
``?mode=ro`` invariant is preserved end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

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
    raw: dict[str, Any]


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

    def _item_url(self, item_key: str) -> str:
        base = self.config.zotero_api_base_url.rstrip("/")
        return f"{base}/users/{self.config.zotero_user_id}/items/{item_key}"

    def _headers(self) -> dict[str, str]:
        return {
            "Zotero-API-Key": self.config.zotero_api_key,
            "Zotero-API-Version": "3",
            "Accept": "application/json",
        }
