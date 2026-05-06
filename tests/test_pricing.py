from randy.providers.pricing import price_for


def test_known_models_have_real_prices():
    p = price_for("claude-opus-4-7")
    assert p.input_per_mtok > 0
    assert p.output_per_mtok > p.input_per_mtok


def test_cost_calc():
    p = price_for("claude-opus-4-7")
    cost = p.cost(1_000_000, 0)
    assert cost == p.input_per_mtok


def test_unknown_model_falls_back():
    p = price_for("nonexistent-model-xyz")
    assert p.input_per_mtok > 0
    assert p.output_per_mtok > 0


def test_typical_session_cost_under_cap():
    """A realistic per-expert turn (10K in / 3K out) should fit under $2/model cap."""
    for model in ["claude-opus-4-7", "gpt-5.2-pro", "gemini-3-pro", "deepseek-v3.2-speciale"]:
        p = price_for(model)
        cost = p.cost(10_000, 3_000) * 2
        assert cost < 2.0, f"{model} two-round cost ${cost:.2f} exceeds $2/model cap"


def test_cache_pricing_discounts_reads():
    p = price_for("claude-sonnet-4-6")
    full = p.cost_with_cache(input_tokens=10_000, output_tokens=0)
    cached = p.cost_with_cache(input_tokens=0, output_tokens=0, cache_read=10_000)
    assert cached < full
    # Cache read at 10% means 10x cheaper for that portion.
    assert abs(cached - full * 0.10) < 1e-6


def test_cache_pricing_charges_writes_more():
    p = price_for("claude-sonnet-4-6")
    full = p.cost_with_cache(input_tokens=10_000, output_tokens=0)
    cache_write = p.cost_with_cache(input_tokens=0, output_tokens=0, cache_create=10_000)
    # Cache write costs 1.25x base.
    assert abs(cache_write - full * 1.25) < 1e-6
