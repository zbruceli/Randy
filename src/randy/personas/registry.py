from dataclasses import dataclass
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


@dataclass
class Persona:
    key: str
    title: str
    provider: str
    one_liner: str
    system_prompt: str


PERSONAS: dict[str, Persona] = {
    "strategist": Persona(
        key="strategist",
        title="The Strategist",
        provider="anthropic",
        one_liner="Frames the problem, maps the option space, names the real trade-offs.",
        system_prompt=_load("strategist"),
    ),
    "contrarian": Persona(
        key="contrarian",
        title="The Contrarian",
        provider="openai",
        one_liner="Stress-tests the plan; finds what the user is fooling themselves about.",
        system_prompt=_load("contrarian"),
    ),
    "operator": Persona(
        key="operator",
        title="The Operator",
        provider="deepseek",
        one_liner="Turns ideas into next Monday's actions; obsessed with what actually ships.",
        system_prompt=_load("operator"),
    ),
    "facilitator": Persona(
        key="facilitator",
        title="The Facilitator",
        provider="google",
        one_liner="Clarifies the question, runs the meeting, synthesizes the verdict.",
        system_prompt=_load("facilitator"),
    ),
}
