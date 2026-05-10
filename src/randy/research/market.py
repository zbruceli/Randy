"""Market data via yfinance.

Synchronous library; we offload to a thread. No paid API needed for the kinds
of questions Randy fields (stock quotes, fundamentals, recent moves).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import yfinance as yf

logger = logging.getLogger("randy.research.market")


@dataclass
class MarketSnapshot:
    symbol: str
    name: str | None
    price: float | None
    currency: str | None
    market_cap: float | None
    pe_ratio: float | None
    week_change_pct: float | None
    summary: str
    as_of: str
    ok: bool
    error: str | None = None


def _to_float(x) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _format_money(amount: float | None, currency: str = "USD") -> str:
    if amount is None:
        return "?"
    if abs(amount) >= 1e12:
        return f"{amount / 1e12:.2f}T {currency}"
    if abs(amount) >= 1e9:
        return f"{amount / 1e9:.2f}B {currency}"
    if abs(amount) >= 1e6:
        return f"{amount / 1e6:.2f}M {currency}"
    return f"{amount:,.2f} {currency}"


def _snapshot_sync(symbol: str) -> MarketSnapshot:
    try:
        ticker = yf.Ticker(symbol)
        info = getattr(ticker, "info", {}) or {}
        hist = ticker.history(period="5d", auto_adjust=False)

        price = _to_float(info.get("regularMarketPrice")) or _to_float(info.get("currentPrice"))
        if price is None and not hist.empty:
            price = _to_float(hist["Close"].iloc[-1])

        # 5-day percent change.
        week_change_pct: float | None = None
        if not hist.empty and len(hist) >= 2:
            first = _to_float(hist["Close"].iloc[0])
            last = _to_float(hist["Close"].iloc[-1])
            if first and last and first != 0:
                week_change_pct = (last - first) / first * 100.0

        currency = info.get("currency") or "USD"
        name = info.get("longName") or info.get("shortName")
        market_cap = _to_float(info.get("marketCap"))
        pe = _to_float(info.get("trailingPE")) or _to_float(info.get("forwardPE"))

        if price is None and not name:
            return MarketSnapshot(
                symbol=symbol, name=None, price=None, currency=currency,
                market_cap=None, pe_ratio=None, week_change_pct=None,
                summary="", as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ok=False, error="no data returned",
            )

        bits = [f"{name or symbol} ({symbol})"]
        if price is not None:
            bits.append(f"price {price:.2f} {currency}")
        if week_change_pct is not None:
            bits.append(f"5d {week_change_pct:+.2f}%")
        if market_cap is not None:
            bits.append(f"mkt cap {_format_money(market_cap, currency)}")
        if pe is not None:
            bits.append(f"P/E {pe:.1f}")
        summary = " · ".join(bits)

        return MarketSnapshot(
            symbol=symbol, name=name, price=price, currency=currency,
            market_cap=market_cap, pe_ratio=pe, week_change_pct=week_change_pct,
            summary=summary, as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ok=True,
        )
    except Exception as e:
        return MarketSnapshot(
            symbol=symbol, name=None, price=None, currency=None,
            market_cap=None, pe_ratio=None, week_change_pct=None,
            summary="", as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ok=False, error=f"{type(e).__name__}: {e}",
        )


async def market_snapshot(symbol: str) -> MarketSnapshot:
    """Async wrapper — yfinance is sync, so we offload to a thread."""
    return await asyncio.to_thread(_snapshot_sync, symbol)
