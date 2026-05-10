"""URL fetcher with main-text extraction via trafilatura."""

import asyncio
import logging
from dataclasses import dataclass

import httpx
import trafilatura

logger = logging.getLogger("randy.research.fetcher")

_DEFAULT_TIMEOUT = 8.0
_USER_AGENT = "Mozilla/5.0 (compatible; RandyBot/0.1; +https://github.com/zbruceli/Randy)"
_MAX_BYTES = 1_500_000  # 1.5 MB cap on raw page download


@dataclass
class FetchResult:
    url: str
    title: str | None
    text: str        # extracted main content (markdown-ish from trafilatura)
    ok: bool
    error: str | None = None


async def fetch_url(url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> FetchResult:
    """Fetch a URL, extract main text. Best-effort — never raises."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text[:_MAX_BYTES]
    except Exception as e:
        return FetchResult(url=url, title=None, text="", ok=False, error=f"{type(e).__name__}: {e}")

    # trafilatura is sync and CPU-bound; offload so we don't stall the loop.
    try:
        extracted = await asyncio.to_thread(
            trafilatura.extract,
            html,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        meta = await asyncio.to_thread(
            trafilatura.extract_metadata,
            html,
        )
    except Exception as e:
        return FetchResult(url=url, title=None, text="", ok=False, error=f"extract: {e}")

    if not extracted:
        return FetchResult(url=url, title=None, text="", ok=False, error="empty extract")

    title = None
    if meta is not None:
        title = getattr(meta, "title", None)

    return FetchResult(url=url, title=title, text=extracted.strip(), ok=True)
