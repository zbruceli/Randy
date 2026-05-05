from dataclasses import dataclass

from ..personas import Persona
from ..providers.base import Provider, ProviderResponse


@dataclass
class Expert:
    persona: Persona
    provider: Provider

    async def respond(
        self,
        brief: str,
        prior_drafts: dict[str, str] | None = None,
        max_tokens: int = 4096,
    ) -> ProviderResponse:
        user_message = brief
        if prior_drafts:
            ctx = "\n\n".join(
                f"--- {key.upper()}'s round-1 draft ---\n{text}" for key, text in prior_drafts.items()
            )
            user_message = (
                f"{brief}\n\n"
                "# Round 2 — critique and revise\n\n"
                "Below are the round-1 drafts from the other experts on this committee. Your job:\n\n"
                "1. **Critique each by name** — identify one substantive thing each one got wrong, "
                "glossed over, or missed. Be specific; quote them if useful.\n"
                "2. **Revise your own position** — incorporate anything they got right that you "
                "missed, and sharpen what you stand by.\n"
                "3. **End with the disagreement that matters** — if you and another expert still "
                "disagree, name it plainly so the user can decide.\n\n"
                "Stay in your persona. Don't soften to be polite.\n\n"
                f"{ctx}"
            )
        return await self.provider.complete(
            system=self.persona.system_prompt,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=max_tokens,
        )
