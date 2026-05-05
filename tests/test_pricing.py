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
