"""Researcher — the pre-R1 grounding pass.

Runs ahead of the experts to extract entities from the question, search the
web (Brave), fetch top results (httpx + trafilatura), pull market data when
tickers are mentioned (yfinance), and distill a research brief that is
injected into every persona's prompt.

Time-bounded: a hard ``timeout`` caps the whole phase. On expiry, whatever
was collected so far is returned and R1 proceeds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..memory import MemoryStore
from ..providers.google_provider import GoogleProvider
from .brave import BraveClient, BraveResult
from .fetcher import FetchResult, fetch_url
from .market import MarketSnapshot, market_snapshot

logger = logging.getLogger("randy.research")

_EXTRACT_PROMPT = """You are extracting research targets from a strategic question \
sent to a personal advisory committee. The committee has three LLM experts who will \
hallucinate company numbers and dates if not grounded in verified facts. Your job: \
find every real-world entity worth looking up.

Today: {today}

The user's question:
{question}

{prior_context}

Default to researching. Skip ONLY if the question is purely about the user's internal \
life with zero real-world hooks (e.g. "should I quit my job?" with no company named, \
"how do I deal with anxiety?"). If a company, person, market, technology, or event is \
named or clearly implied, look it up.

Return JSON ONLY (no prose, no markdown fences) with these keys:
  - "topics": real-world entities the experts need facts about. Companies, people, \
markets, technologies, regulations, events. 0 to {max_topics} items.
  - "search_queries": specific web search queries — one per topic, plus optional \
1-2 for cross-topic context (e.g. recent industry news). Phrase the way a search \
engine expects. Cap at {max_queries}. Include the year if recency matters.
  - "tickers": stock tickers to fetch market data for (e.g. ["AAPL", "MYRG"]). \
Only include if you're confident the symbol exists and matches the entity.

Examples:
  Q: "Should I invest in NVIDIA?"
  A: {{"topics": ["NVIDIA", "AI chip market"], "search_queries": ["NVIDIA Q4 2025 earnings revenue", "AI accelerator market share 2026", "NVIDIA stock outlook 2026"], "tickers": ["NVDA"]}}

  Q: "Should I quit to start a SaaS company?"
  A: {{"topics": ["SaaS startup market"], "search_queries": ["SaaS funding environment 2026", "B2B SaaS growth trends 2026"], "tickers": []}}

  Q: "How should I prep for next week's offsite?"
  A: {{"topics": [], "search_queries": [], "tickers": []}}

Strict JSON only."""


_DISTILL_PROMPT = """You are distilling research findings for a strategic advisory \
committee. Be concise, attribute every claim to a source, and flag uncertainty.

Today: {today}

User's question:
{question}

# Tools used
{tool_summary}

# Search results
{search_section}

# Fetched pages (extracted main text)
{fetched_section}

# Market data
{market_section}

# Your job
Produce a research brief in markdown. Format:

## <Topic>
- <claim> [<short source name>]
- <claim> [<short source name>]
- ...

Rules:
- Attribute every claim to a source — bracketed at end of bullet.
- Mark uncertainty as [reported] or [estimated] when the source itself is hedged.
- Skip claims you can't attribute to a fetched page or search snippet.
- Prefer recent data; note dates if material.
- Keep the whole brief under 1500 words.
- If there is genuinely nothing to report, say "No external research was needed for this question."

Output the markdown brief only."""


@dataclass
class ResearchSource:
    url: str
    title: str
    text_excerpt: str          # short snippet (first ~500 chars of extracted main text)


@dataclass
class ResearchBrief:
    markdown: str              # the distilled brief, injected into expert prompts
    topics: list[str] = field(default_factory=list)
    sources: list[ResearchSource] = field(default_factory=list)
    market_snapshots: list[MarketSnapshot] = field(default_factory=list)
    cost_usd: float = 0.0
    duration_s: float = 0.0
    timed_out: bool = False
    notes: str = ""            # human-readable summary of what was collected

    def is_empty(self) -> bool:
        return not (self.sources or self.market_snapshots or self.markdown.strip())


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        first, last = text.find("{"), text.rfind("}")
        if first >= 0 and last > first:
            try:
                return json.loads(text[first : last + 1])
            except json.JSONDecodeError:
                pass
    return {}


def _classify_volatility(topic: str, claim: str) -> str:
    blob = f"{topic} {claim}".lower()
    if any(k in blob for k in ("price", "stock", "rate", "yield", "today", "this week")):
        return "volatile"
    if any(k in blob for k in ("founded", "headquartered", "born", "definition", "history")):
        return "evergreen"
    return "slow"


def _save_raw(research_dir: Path, fr: FetchResult) -> Path | None:
    if not fr.ok:
        return None
    try:
        research_dir.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha1(fr.url.encode()).hexdigest()[:12]
        path = research_dir / f"{h}.md"
        body = f"# {fr.title or fr.url}\n\nSource: {fr.url}\n\n---\n\n{fr.text}"
        path.write_text(body, encoding="utf-8")
        return path
    except Exception as e:
        logger.warning("failed to write %s: %s", fr.url, e)
        return None


def _save_index(research_dir: Path, sources: list[ResearchSource], market: list[MarketSnapshot]) -> None:
    try:
        research_dir.mkdir(parents=True, exist_ok=True)
        lines = ["url,title,kind"]
        for s in sources:
            safe_title = (s.title or "").replace('"', "'").replace(",", " ")
            lines.append(f'"{s.url}","{safe_title}",page')
        for m in market:
            lines.append(f'yfinance:{m.symbol},"{m.name or m.symbol}",ticker')
        (research_dir / "index.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("failed to write index.csv: %s", e)


class Researcher:
    def __init__(self, store: MemoryStore | None = None):
        self.store = store
        self._brave = BraveClient(settings.brave_api_key) if settings.brave_api_key else None
        self._gemini = (
            GoogleProvider(settings.google_api_key, settings.researcher_model)
            if settings.google_api_key
            else None
        )

    async def _extract_targets(self, question: str, prior_context: str) -> dict:
        if self._gemini is None:
            return {"topics": [], "search_queries": [], "tickers": []}
        prompt = _EXTRACT_PROMPT.format(
            today=_today(),
            question=question,
            prior_context=prior_context or "",
            max_topics=settings.research_max_topics,
            max_queries=settings.research_max_topics + 2,
        )
        try:
            resp = await self._gemini.complete(
                system="You extract structured research targets. Return strict JSON only.",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,  # Gemini Flash burns thinking tokens; need headroom.
            )
        except Exception as e:
            logger.warning("entity extraction failed: %s", e)
            return {"topics": [], "search_queries": [], "tickers": []}
        out = _extract_json(resp.text or "")
        if not out:
            logger.warning(
                "extractor returned unparseable JSON; raw=%r", (resp.text or "")[:300]
            )
        return out

    async def _distill(
        self,
        *,
        question: str,
        searches: dict[str, list[BraveResult]],
        fetches: list[FetchResult],
        market: list[MarketSnapshot],
        topics: list[str],
    ) -> tuple[str, float]:
        if self._gemini is None:
            return ("Research disabled — no GOOGLE_API_KEY configured.", 0.0)

        # Format the inputs for the prompt.
        search_blocks: list[str] = []
        for query, results in searches.items():
            search_blocks.append(f"### Query: {query}")
            for r in results:
                search_blocks.append(f"- [{r.title}]({r.url}) — {r.description}")
        search_section = "\n".join(search_blocks) if search_blocks else "(none)"

        fetched_blocks: list[str] = []
        for f in fetches:
            if not f.ok or not f.text:
                continue
            fetched_blocks.append(f"### {f.title or f.url}\nSource: {f.url}\n\n{f.text[:3000]}")
        fetched_section = "\n\n---\n\n".join(fetched_blocks) if fetched_blocks else "(none)"

        market_blocks = [f"- {m.summary} (as of {m.as_of[:10]})" for m in market if m.ok]
        market_section = "\n".join(market_blocks) if market_blocks else "(none)"

        tool_summary = (
            f"- Brave searches: {len(searches)} ({sum(len(v) for v in searches.values())} results)\n"
            f"- URLs fetched: {sum(1 for f in fetches if f.ok)}/{len(fetches)}\n"
            f"- Tickers: {len(market)}"
        )

        prompt = _DISTILL_PROMPT.format(
            today=_today(),
            question=question,
            tool_summary=tool_summary,
            search_section=search_section,
            fetched_section=fetched_section,
            market_section=market_section,
        )

        try:
            resp = await self._gemini.complete(
                system="You distill research findings into concise, attributed briefs.",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
            )
        except Exception as e:
            logger.warning("distillation failed: %s", e)
            return (f"Research distillation failed: {e}", 0.0)
        return (resp.text or "", resp.cost_usd)

    async def _run_unbounded(
        self,
        question: str,
        prior_context: str,
        session_id: str | None,
        on_progress=None,
    ) -> ResearchBrief:
        started = asyncio.get_event_loop().time()
        cost = 0.0

        if self._brave is None:
            logger.info("BRAVE_API_KEY missing; research phase will skip web search")
        if on_progress:
            await on_progress("  · Researcher: extracting targets…")

        targets = await self._extract_targets(question, prior_context)
        topics = (targets.get("topics") or [])[: settings.research_max_topics]
        queries = (targets.get("search_queries") or [])[: settings.research_max_topics + 2]
        tickers = list({t.upper() for t in (targets.get("tickers") or []) if isinstance(t, str)})[:6]

        if not topics and not queries and not tickers:
            return ResearchBrief(
                markdown="No external research was needed for this question.",
                topics=[],
                sources=[],
                market_snapshots=[],
                cost_usd=cost,
                duration_s=asyncio.get_event_loop().time() - started,
                notes="researcher decided no external data needed",
            )

        if on_progress:
            await on_progress(
                f"  · Researcher: {len(queries)} search(es), {len(tickers)} ticker(s)…"
            )

        # Web searches in parallel.
        searches: dict[str, list[BraveResult]] = {}
        if self._brave and queries:
            search_tasks = [self._brave.search(q, count=settings.research_max_results_per_topic) for q in queries]
            search_outs = await asyncio.gather(*search_tasks, return_exceptions=True)
            for q, out in zip(queries, search_outs):
                if isinstance(out, Exception):
                    logger.warning("search %r failed: %s", q, out)
                    continue
                searches[q] = out

        # Pick top URLs across all searches (dedupe), fetch in parallel.
        seen_urls: set[str] = set()
        urls_to_fetch: list[str] = []
        for results in searches.values():
            for r in results:
                if r.url in seen_urls:
                    continue
                seen_urls.add(r.url)
                urls_to_fetch.append(r.url)
        # Cap fetches to avoid blowing the time budget.
        urls_to_fetch = urls_to_fetch[: settings.research_max_topics * settings.research_max_results_per_topic]

        fetches: list[FetchResult] = []
        if urls_to_fetch:
            fetch_outs = await asyncio.gather(*(fetch_url(u) for u in urls_to_fetch))
            fetches = list(fetch_outs)

        # Market snapshots (parallel).
        market: list[MarketSnapshot] = []
        if tickers:
            market_outs = await asyncio.gather(*(market_snapshot(t) for t in tickers))
            market = [m for m in market_outs if m.ok]

        if on_progress:
            ok_pages = sum(1 for f in fetches if f.ok)
            await on_progress(
                f"  · Researcher: distilling ({ok_pages}/{len(fetches)} pages, {len(market)} ticker(s))…"
            )

        markdown, distill_cost = await self._distill(
            question=question,
            searches=searches,
            fetches=fetches,
            market=market,
            topics=topics,
        )
        cost += distill_cost

        # Build sources list and persist raw + index.
        sources: list[ResearchSource] = []
        if session_id:
            research_dir = Path(settings.research_dir) / session_id
            for f in fetches:
                if not f.ok:
                    continue
                _save_raw(research_dir, f)
                sources.append(
                    ResearchSource(
                        url=f.url,
                        title=f.title or f.url,
                        text_excerpt=f.text[:500],
                    )
                )
            _save_index(research_dir, sources, market)

            # Persist facts to DB. We don't try to atomize the brief — instead store
            # one fact per fetched source (claim = title) and one per market snapshot.
            if self.store:
                for f in fetches:
                    if not f.ok:
                        continue
                    fact_id = uuid.uuid4().hex[:12]
                    topic = (f.title or f.url)[:60]
                    self.store.upsert_fact(
                        fact_id=fact_id,
                        session_id=session_id,
                        topic=topic,
                        claim=f.title or f.url,
                        source_url=f.url,
                        source_title=f.title,
                        raw_excerpt=f.text[:1000],
                        volatility=_classify_volatility(topic, f.text[:500]),
                        confidence="reported",
                    )
                for m in market:
                    fact_id = uuid.uuid4().hex[:12]
                    self.store.upsert_fact(
                        fact_id=fact_id,
                        session_id=session_id,
                        topic=m.symbol,
                        claim=m.summary,
                        source_url=f"https://finance.yahoo.com/quote/{m.symbol}",
                        source_title=f"yfinance: {m.symbol}",
                        raw_excerpt=None,
                        volatility="volatile",
                        confidence="verified",
                    )
        else:
            for f in fetches:
                if not f.ok:
                    continue
                sources.append(
                    ResearchSource(
                        url=f.url,
                        title=f.title or f.url,
                        text_excerpt=f.text[:500],
                    )
                )

        return ResearchBrief(
            markdown=markdown.strip() or "Research distillation produced no output.",
            topics=topics,
            sources=sources,
            market_snapshots=market,
            cost_usd=cost,
            duration_s=asyncio.get_event_loop().time() - started,
            notes=f"{len(searches)} searches · {sum(1 for f in fetches if f.ok)} pages · {len(market)} tickers",
        )

    async def run(
        self,
        question: str,
        *,
        prior_context: str = "",
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        on_progress=None,
    ) -> ResearchBrief:
        """Run the research phase, capped at ``timeout_seconds``.

        On timeout, returns a brief noting the timeout. The caller proceeds with
        whatever was injected so far (in v1: nothing — partial results aren't
        plumbed out of the cancelled task).
        """
        timeout = timeout_seconds or settings.research_timeout_seconds
        started = asyncio.get_event_loop().time()
        try:
            brief = await asyncio.wait_for(
                self._run_unbounded(question, prior_context, session_id, on_progress),
                timeout=timeout,
            )
            return brief
        except asyncio.TimeoutError:
            elapsed = asyncio.get_event_loop().time() - started
            logger.warning("research phase timed out after %.1fs (cap %.1fs)", elapsed, timeout)
            return ResearchBrief(
                markdown="(Research phase timed out — proceeding without external grounding.)",
                duration_s=elapsed,
                timed_out=True,
                notes=f"timeout after {timeout:.0f}s",
            )
        except Exception as e:
            logger.exception("research phase failed")
            return ResearchBrief(
                markdown=f"(Research phase errored: {type(e).__name__})",
                duration_s=asyncio.get_event_loop().time() - started,
                notes=f"error: {e}",
            )
