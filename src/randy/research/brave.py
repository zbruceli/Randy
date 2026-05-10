"""Brave Search API client.

Free tier: 2k queries/month. Endpoint:
  https://api.search.brave.com/res/v1/web/search?q=<query>
Header: X-Subscription-Token.
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("randy.research.brave")

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


@dataclass
class BraveResult:
    title: str
    url: str
    description: str


class BraveClient:
    def __init__(self, api_key: str, timeout: float = 8.0):
        if not api_key:
            raise ValueError("BRAVE_API_KEY is required")
        self.api_key = api_key
        self.timeout = timeout

    async def search(self, query: str, *, count: int = 5) -> list[BraveResult]:
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        params = {"q": query, "count": count, "safesearch": "off"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(_ENDPOINT, headers=headers, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.warning("brave search failed for %r: %s", query, e)
                return []
        data = resp.json()
        web = data.get("web", {}) or {}
        results = web.get("results", []) or []
        out: list[BraveResult] = []
        for r in results[:count]:
            url = r.get("url")
            if not url:
                continue
            out.append(
                BraveResult(
                    title=(r.get("title") or "").strip(),
                    url=url,
                    description=(r.get("description") or "").strip(),
                )
            )
        return out
