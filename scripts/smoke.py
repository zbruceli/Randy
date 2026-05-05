"""Smoke test: ping every configured vendor with a tiny prompt, print tokens + cost.

Usage:
    python scripts/smoke.py

Skips any vendor whose API key is not set in .env.
"""

import asyncio
import sys
import time
import traceback

from randy.config import settings
from randy.providers.anthropic_provider import AnthropicProvider
from randy.providers.cost_meter import CostMeter
from randy.providers.deepseek_provider import DeepSeekProvider
from randy.providers.google_provider import GoogleProvider
from randy.providers.openai_provider import OpenAIProvider

PROMPT = "How many r's are in the word 'strawberry'? Answer with the number and a one-sentence explanation."
SYSTEM = "You are a careful, precise assistant."


def _build_providers() -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if settings.anthropic_api_key:
        out.append(
            ("anthropic", AnthropicProvider(settings.anthropic_api_key, settings.expert_anthropic_model))
        )
    if settings.openai_api_key:
        out.append(
            (
                "openai",
                OpenAIProvider(
                    settings.openai_api_key,
                    settings.expert_openai_model,
                    api="responses" if "pro" in settings.expert_openai_model.lower() else "chat",
                ),
            )
        )
    if settings.google_api_key:
        out.append(
            ("google", GoogleProvider(settings.google_api_key, settings.facilitator_model))
        )
    if settings.deepseek_api_key:
        out.append(
            ("deepseek", DeepSeekProvider(settings.deepseek_api_key, settings.expert_deepseek_model))
        )
    return out


async def _ping(name: str, provider) -> dict:
    t0 = time.monotonic()
    try:
        resp = await provider.complete(
            system=SYSTEM,
            messages=[{"role": "user", "content": PROMPT}],
            max_tokens=2048,
        )
        return {
            "name": name,
            "model": resp.model,
            "ok": True,
            "latency_s": round(time.monotonic() - t0, 2),
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cost_usd": resp.cost_usd,
            "text": resp.text.strip(),
        }
    except Exception as e:
        return {
            "name": name,
            "ok": False,
            "latency_s": round(time.monotonic() - t0, 2),
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc(),
        }


async def main() -> int:
    providers = _build_providers()
    if not providers:
        print("No API keys set. Populate .env from .env.example.", file=sys.stderr)
        return 2

    print(f"Pinging {len(providers)} vendor(s) in parallel: {[n for n, _ in providers]}\n")
    results = await asyncio.gather(*(_ping(n, p) for n, p in providers))

    meter = CostMeter(
        session_cap_usd=settings.session_cost_cap_usd,
        per_model_cap_usd=settings.per_model_cost_cap_usd,
    )

    width = max(len(r["name"]) for r in results)
    failures = 0
    for r in results:
        if r["ok"]:
            meter.record(r["model"], r["cost_usd"])
            print(
                f"{r['name']:<{width}}  {r['model']:<32}  "
                f"{r['latency_s']:>5.2f}s  "
                f"in={r['input_tokens']:>5}  out={r['output_tokens']:>5}  "
                f"${r['cost_usd']:.5f}"
            )
            print(f"  → {r['text']}\n")
        else:
            failures += 1
            print(f"{r['name']:<{width}}  FAILED in {r['latency_s']}s — {r['error']}\n")

    print("─" * 60)
    print(f"Total cost: ${meter.total:.5f}    (session cap ${settings.session_cost_cap_usd})")
    print(f"By model:   {meter.by_model}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
