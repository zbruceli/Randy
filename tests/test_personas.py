from randy.personas import PERSONAS


def test_all_four_personas_load():
    expected = {"strategist", "contrarian", "operator", "facilitator"}
    assert set(PERSONAS) == expected


def test_personas_have_substantive_prompts():
    for key, p in PERSONAS.items():
        assert len(p.system_prompt) > 500, f"{key} prompt looks like a stub"
        assert p.provider in {"anthropic", "openai", "google", "deepseek"}
        assert p.one_liner


def test_distinct_providers():
    providers = [p.provider for p in PERSONAS.values()]
    assert len(set(providers)) == 4, "each persona should map to a distinct vendor"
