from dataclasses import dataclass, field


class CostCapExceeded(Exception):
    pass


@dataclass
class CostMeter:
    session_cap_usd: float
    per_model_cap_usd: float
    by_model: dict[str, float] = field(default_factory=dict)
    total: float = 0.0

    def record(self, model: str, cost_usd: float) -> None:
        self.by_model[model] = self.by_model.get(model, 0.0) + cost_usd
        self.total += cost_usd
        if self.total > self.session_cap_usd:
            raise CostCapExceeded(f"session cap ${self.session_cap_usd} exceeded: ${self.total:.2f}")
        if self.by_model[model] > self.per_model_cap_usd:
            raise CostCapExceeded(
                f"per-model cap ${self.per_model_cap_usd} exceeded for {model}: ${self.by_model[model]:.2f}"
            )
