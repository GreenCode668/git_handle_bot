"""Async GitHub REST client with retries, pagination and redacted errors."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from ghbot.logging_setup import get_redactor

log = logging.getLogger(__name__)

API_VERSION = "2022-11-28"
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(get_redactor()(f"GitHub API error {status}: {message}"))
        self.status = status


class GitHubNotFound(GitHubError):
    pass


@dataclass
class Page:
    items: list[dict[str, Any]]
    page: int
    has_next: bool
    last_page: int | None


class GitHubClient:
    def __init__(self, token: str, base_url: str = "https://api.github.com", *, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "ghbot-telegram",
            },
            follow_redirects=True,
        )
        self.last_scopes: str | None = None
        self.rate_remaining: int | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                      json: Any = None, retries: int = 3) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                if attempt > retries:
                    raise GitHubError(0, f"network error: {type(exc).__name__}") from None
                await asyncio.sleep(2**attempt)
                continue

            if "X-OAuth-Scopes" in response.headers:
                self.last_scopes = response.headers["X-OAuth-Scopes"]
            if "X-RateLimit-Remaining" in response.headers:
                self.rate_remaining = int(response.headers["X-RateLimit-Remaining"])

            retryable = response.status_code >= 500 or response.status_code == 429 or (
                response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0"
            ) or (response.status_code == 403 and "retry-after" in response.headers)
            # Mutations are retried only on 5xx before any processing; never on 403/429 bursts.
            if retryable and attempt <= retries and (method == "GET" or response.status_code in (502, 503, 504)):
                delay = min(int(response.headers.get("retry-after", 2**attempt)), 60)
                log.warning("GitHub %s %s -> %s, retrying in %ss", method, path, response.status_code, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                message = _error_message(response)
                if response.status_code == 404:
                    raise GitHubNotFound(404, message)
                raise GitHubError(response.status_code, message)
            return response

    async def get(self, path: str, **params: Any) -> Any:
        response = await self.request("GET", path, params=params or None)
        return response.json() if response.content else None

    async def page(self, path: str, *, page: int = 1, per_page: int = 30, **params: Any) -> Page:
        response = await self.request("GET", path, params={**params, "page": page, "per_page": per_page})
        data = response.json() if response.content else []  # e.g. 204 for contributors of an empty repo
        items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
        links = dict((rel, url) for url, rel in _LINK_RE.findall(response.headers.get("Link", "")))
        last_page = None
        if "last" in links:
            match = re.search(r"[?&]page=(\d+)", links["last"])
            last_page = int(match.group(1)) if match else None
        elif "next" not in links:
            last_page = page
        return Page(items=items, page=page, has_next="next" in links, last_page=last_page)

    async def paginate(self, path: str, *, max_items: int = 1000, per_page: int = 100, **params: Any) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page_no = 1
        while len(results) < max_items:
            page = await self.page(path, page=page_no, per_page=per_page, **params)
            results.extend(page.items)
            if not page.has_next:
                break
            page_no += 1
        return results[:max_items]

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await self.request("POST", "/graphql", json={"query": query, "variables": variables})
        data = response.json()
        if data.get("errors"):
            raise GitHubError(200, "; ".join(e.get("message", "unknown") for e in data["errors"]))
        return data["data"]


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "unknown error"
    message = body.get("message", "unknown error")
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        details = [e.get("message") or e.get("code") for e in errors if isinstance(e, dict)]
        message += " (" + "; ".join(d for d in details if d) + ")"
    return message
