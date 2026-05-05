import pytest

from randy.providers.cost_meter import CostCapExceeded, CostMeter


def test_records_and_sums():
    m = CostMeter(session_cap_usd=25.0, per_model_cap_usd=2.0)
    m.record("a", 0.5)
    m.record("a", 0.3)
    m.record("b", 1.0)
    assert m.total == pytest.approx(1.8)
    assert m.by_model["a"] == pytest.approx(0.8)


def test_session_cap():
    m = CostMeter(session_cap_usd=1.0, per_model_cap_usd=10.0)
    m.record("a", 0.6)
    with pytest.raises(CostCapExceeded):
        m.record("b", 0.5)


def test_per_model_cap():
    m = CostMeter(session_cap_usd=100.0, per_model_cap_usd=1.0)
    m.record("a", 0.6)
    with pytest.raises(CostCapExceeded):
        m.record("a", 0.5)
