"""framework/plugins/registry.py -- the plugin marketplace client.

The marketplace is an ordinary GitHub repository (``hilleywyn/archimedes-plugins``
by default). It holds an ``index.json`` catalogue and a ``plugins/`` directory
of ``.lua`` files. This module reads both through the GitHub contents API, so
the same code path works for public and private marketplaces (a private one
just needs ``GITHUB_TOKEN`` set).

The index is cached briefly so a burst of ``.ai plugins search`` calls does
not hammer the API.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import time

import aiohttp

log = logging.getLogger(__name__)

_API_ROOT = "https://api.github.com/repos"
_INDEX_TTL_S = 300
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)


class RegistryError(Exception):
    """The marketplace could not be reached or returned something unusable."""


class PluginRegistry:
    """Reads the plugin catalogue and plugin sources from a GitHub repo."""

    def __init__(self, repo: str, ref: str = "main", token: str = "") -> None:
        self.repo = repo.strip().strip("/")
        self.ref = ref.strip() or "main"
        self._token = token.strip()
        self._index_cache: list[dict] | None = None
        self._index_cached_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.repo and "/" in self.repo)

    def _headers(self) -> dict:
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "Archimedes-Plugin-Manager"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get_file(self, path: str) -> str:
        """Fetch one repository file's text through the contents API."""
        url = f"{_API_ROOT}/{self.repo}/contents/{path}"
        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.get(
                    url, headers=self._headers(), params={"ref": self.ref},
                ) as resp:
                    if resp.status == 404:
                        raise RegistryError(f"`{path}` is not in the marketplace.")
                    if resp.status == 403:
                        raise RegistryError(
                            "the marketplace API rate limit was hit -- set "
                            "GITHUB_TOKEN to raise it."
                        )
                    if resp.status != 200:
                        raise RegistryError(
                            f"marketplace returned HTTP {resp.status}."
                        )
                    payload = await resp.json()
        except aiohttp.ClientError as exc:
            raise RegistryError(f"could not reach the marketplace: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RegistryError("marketplace sent a malformed response.") from exc

        content = payload.get("content")
        if not content or payload.get("encoding") != "base64":
            raise RegistryError(f"`{path}` could not be decoded.")
        try:
            return base64.b64decode(content).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise RegistryError(f"`{path}` is not valid UTF-8 text.") from exc

    async def fetch_index(self, *, force: bool = False) -> list[dict]:
        """Return the marketplace catalogue, using the short-lived cache."""
        now = time.monotonic()
        if (not force and self._index_cache is not None
                and now - self._index_cached_at < _INDEX_TTL_S):
            return self._index_cache
        raw = await self._get_file("index.json")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RegistryError("the marketplace index is not valid JSON.") from exc
        plugins = data.get("plugins") if isinstance(data, dict) else data
        if not isinstance(plugins, list):
            raise RegistryError("the marketplace index has no plugin list.")
        self._index_cache = plugins
        self._index_cached_at = now
        return plugins

    async def search(self, query: str) -> list[dict]:
        """Catalogue entries whose id / name / description match ``query``."""
        plugins = await self.fetch_index()
        needle = (query or "").strip().lower()
        if not needle:
            return list(plugins)
        hits = []
        for entry in plugins:
            haystack = " ".join(str(entry.get(k, "")) for k in
                                ("id", "name", "description", "category", "author"))
            if needle in haystack.lower():
                hits.append(entry)
        return hits

    async def get_entry(self, plugin_id: str) -> dict | None:
        """The catalogue entry for one plugin id, or ``None``."""
        for entry in await self.fetch_index():
            if str(entry.get("id")) == plugin_id:
                return entry
        return None

    async def fetch_source(self, plugin_id: str) -> tuple[dict, str]:
        """Return ``(catalogue_entry, lua_source)`` for one plugin id."""
        entry = await self.get_entry(plugin_id)
        if entry is None:
            raise RegistryError(f"no plugin `{plugin_id}` in the marketplace.")
        path = str(entry.get("path") or f"plugins/{plugin_id}.lua")
        return entry, await self._get_file(path)
