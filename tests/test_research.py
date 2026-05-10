"""Tests for the researcher and fact storage.

External APIs (Brave, yfinance, Google) are stubbed; we exercise the
orchestration logic and persistence, not the network."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from randy.memory import MemoryStore
from randy.research import Researcher
from randy.research.brave import BraveResult
from randy.research.fetcher import FetchResult
from randy.research.market import MarketSnapshot


@pytest.fixture
def store():
    db = tempfile.mktemp(suffix=".sqlite")
    s = MemoryStore(db)
    s.ensure_user("u1")
    s.start_session("sess1", "u1", "topic")
    yield s
    os.unlink(db)


def test_facts_crud_isolated(store):
    store.upsert_fact(
        fact_id="f1", session_id="sess1", topic="MYRG",
        claim="MYRG FY24 revenue 1.2B", source_url="http://x",
    )
    store.upsert_fact(
        fact_id="f2", session_id="sess1", topic="MYRG",
        claim="Q3 EPS beat", source_url="http://y", volatility="volatile",
    )
    store.upsert_fact(
        fact_id="f3", session_id="sess1", topic="Anthropic",
        claim="Latest funding round 2025", source_url="http://z",
    )
    assert len(store.find_facts_by_topic("MYRG")) == 2
    assert len(store.find_facts_by_topic("Anthropic")) == 1
    assert len(store.session_facts("sess1")) == 3
    summary = store.topics_summary()
    topics = {row["topic"]: row["fact_count"] for row in summary}
    assert topics == {"MYRG": 2, "Anthropic": 1}


def test_upsert_overwrites_same_id(store):
    store.upsert_fact(fact_id="f1", session_id="sess1", topic="X", claim="v1")
    store.upsert_fact(fact_id="f1", session_id="sess1", topic="X", claim="v2")
    facts = store.find_facts_by_topic("X")
    assert len(facts) == 1
    assert facts[0].claim == "v2"


@pytest.mark.asyncio
async def test_researcher_no_targets_returns_empty(store):
    r = Researcher(store=store)
    with patch.object(r, "_extract_targets", new=AsyncMock(return_value={
        "topics": [], "search_queries": [], "tickers": []
    })):
        brief = await r.run("how do I deal with anxiety?", session_id="sess1")
    assert brief.markdown.startswith("No external research")
    assert brief.sources == []
    assert brief.market_snapshots == []
    assert not brief.timed_out
    assert store.session_facts("sess1") == []


@pytest.mark.asyncio
async def test_researcher_full_path(store, tmp_path, monkeypatch):
    """Stub Brave + fetch + market + Gemini distillation; verify end-to-end."""
    from randy import config as config_mod
    monkeypatch.setattr(config_mod.settings, "research_dir", str(tmp_path))

    r = Researcher(store=store)
    targets = {
        "topics": ["NVIDIA"],
        "search_queries": ["NVIDIA Q4 2025 earnings"],
        "tickers": ["NVDA"],
    }
    brave_results = [
        BraveResult(title="NVIDIA Q4 results", url="https://example.com/nv1", description="..."),
        BraveResult(title="Capex outlook", url="https://example.com/nv2", description="..."),
    ]
    fetch_results = [
        FetchResult(url="https://example.com/nv1", title="NVIDIA Q4 results",
                    text="NVIDIA reported $X revenue in Q4 2025...", ok=True),
        FetchResult(url="https://example.com/nv2", title="Capex outlook",
                    text="AI capex is forecast to grow...", ok=True),
    ]
    snapshot = MarketSnapshot(
        symbol="NVDA", name="NVIDIA Corp", price=900.0, currency="USD",
        market_cap=2.2e12, pe_ratio=70.0, week_change_pct=2.5,
        summary="NVIDIA Corp (NVDA) · price 900.00 USD · 5d +2.50%",
        as_of="2026-05-10T00:00:00+00:00", ok=True,
    )

    async def fake_brave_search(query, count=5):
        return brave_results

    async def fake_fetch(url, timeout=8.0):
        return next(f for f in fetch_results if f.url == url)

    async def fake_market(sym):
        return snapshot

    async def fake_distill(self_, *, question, searches, fetches, market, topics):
        return ("## NVIDIA\n- Q4 revenue [NVIDIA Q4 results]\n", 0.001)

    with patch.object(r, "_extract_targets", new=AsyncMock(return_value=targets)), \
         patch.object(r._brave, "search", side_effect=fake_brave_search) if r._brave else patch("builtins.dict"), \
         patch("randy.research.researcher.fetch_url", side_effect=fake_fetch), \
         patch("randy.research.researcher.market_snapshot", side_effect=fake_market), \
         patch.object(Researcher, "_distill", new=fake_distill):
        if r._brave is None:
            pytest.skip("BRAVE_API_KEY not set; skipping the full-path test")
        brief = await r.run("Should I invest in NVIDIA?", session_id="sess1")

    assert "NVIDIA" in brief.markdown
    assert len(brief.sources) == 2
    assert len(brief.market_snapshots) == 1
    facts = store.session_facts("sess1")
    # 2 from fetched pages + 1 from market = 3
    assert len(facts) == 3
    nvda_facts = [f for f in facts if f.topic == "NVDA"]
    assert len(nvda_facts) == 1
    assert nvda_facts[0].volatility == "volatile"


def test_market_snapshot_handles_failure():
    """yfinance returning empty/error → ok=False, no crash."""
    from randy.research.market import _snapshot_sync
    snap = _snapshot_sync("ZZZZINVALID")
    assert snap.ok is False or snap.price is None
