"""HTTP client for the Jarvis Command Store API.

Wraps store API calls with household JWT auth. Falls back gracefully
if the store URL is not configured.
"""

from __future__ import annotations

from typing import Any

import httpx
from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

DEFAULT_STORE_URL = "https://store.jarvis.local"  # Override via config


class CommandStoreClient:
    """Client for the command store API."""

    def __init__(self, store_url: str | None = None, jwt_token: str | None = None, household_id: str | None = None):
        self.store_url = (store_url or DEFAULT_STORE_URL).rstrip("/")
        self.jwt_token = jwt_token
        self.household_id = household_id
        self._client = httpx.Client(timeout=30.0)

    def _auth_headers(self) -> dict[str, str]:
        """Build auth headers for household JWT requests."""
        headers: dict[str, str] = {}
        if self.jwt_token:
            headers["Authorization"] = f"Bearer {self.jwt_token}"
        if self.household_id:
            headers["X-Household-Id"] = self.household_id
        return headers

    def search(
        self,
        query: str | None = None,
        category: str | None = None,
        sort: str = "popular",
        page: int = 1,
        per_page: int = 20,
    ) -> dict[str, Any]:
        """Search the command catalog.

        Returns:
            Dict with 'commands', 'total', 'page', 'per_page'.
        """
        params: dict[str, Any] = {"sort": sort, "page": page, "per_page": per_page}
        if query:
            params["q"] = query
        if category:
            params["category"] = category

        resp = self._client.get(f"{self.store_url}/v1/commands", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_command(self, command_name: str) -> dict[str, Any]:
        """Get full details for a command."""
        resp = self._client.get(f"{self.store_url}/v1/commands/{command_name}")
        resp.raise_for_status()
        return resp.json()

    def get_versions(self, command_name: str) -> dict[str, Any]:
        """List versions for a command."""
        resp = self._client.get(f"{self.store_url}/v1/commands/{command_name}/versions")
        resp.raise_for_status()
        return resp.json()

    def get_download_info(self, command_name: str, version: str | None = None) -> dict[str, Any]:
        """Get download info (requires JWT auth).

        Returns:
            Dict with 'github_repo_url', 'git_tag', 'manifest', etc.
        """
        params: dict[str, str] = {}
        if version:
            params["version"] = version

        resp = self._client.get(
            f"{self.store_url}/v1/commands/{command_name}/download",
            params=params,
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def report_install(self, command_name: str) -> None:
        """Report a command installation (fire-and-forget)."""
        try:
            self._client.post(
                f"{self.store_url}/v1/commands/{command_name}/installed",
                headers=self._auth_headers(),
            )
        except Exception as e:
            logger.debug("Failed to report install", command=command_name, error=str(e))

    def get_categories(self) -> list[dict[str, Any]]:
        """List categories with counts."""
        resp = self._client.get(f"{self.store_url}/v1/categories")
        resp.raise_for_status()
        return resp.json().get("categories", [])

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
