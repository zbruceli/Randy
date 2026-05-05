"""Conservative profile updater.

After a consultation, ask Gemini to extract durable updates to the user's
profile. Returns a partial-profile dict — the merge is done in-store using
union semantics, so missing fields keep their current values.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..memory.profile import UserProfile
from ..providers.google_provider import GoogleProvider

logger = logging.getLogger("randy.profile_updater")


PROMPT = """You are updating Randy's stored profile for a user. The profile is what makes Randy feel personal across sessions, so be CONSERVATIVE.

Only persist things that are DURABLE — things that will still be true and useful in 3 months. NOT this session's question, NOT temporary feelings, NOT speculation.

Today's date: {today}

# Current profile (JSON)
{current}

# Today's question
{question}

# Today's synthesis (the recommendation Randy gave)
{synthesis}

# Your task
Return a JSON object with ONLY the fields that should change. Possible fields:
- "goals" (list of strings)
- "constraints" (list of strings)
- "facts" (object: {{key: value}}, e.g. role, industry, location, family situation)
- "decisions" (list of objects: {{date, what, why}})
- "things_tried" (list of objects: {{date, what, outcome}})
- "open_questions" (list of strings)
- "notes" (free-form summary; will OVERWRITE existing notes if non-empty)

Rules:
- Use lists for additions only — existing entries are preserved by the merge.
- Add date "{today}" to any decisions or things_tried you record.
- The user must have stated or clearly implied something for it to count as a fact. Don't infer.
- If nothing durable came up this session, return {{}}.

Output ONLY a JSON object. No prose, no markdown fences."""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Best-effort: find the outermost {...}
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            try:
                return json.loads(text[first : last + 1])
            except json.JSONDecodeError:
                pass
        logger.warning("could not parse profile update; returning empty: %r", text[:200])
        return {}


async def extract_profile_update(
    profile: UserProfile,
    question: str,
    synthesis: str,
) -> tuple[dict[str, Any], float]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = PROMPT.format(
        today=today,
        current=profile.to_json(),
        question=question,
        synthesis=synthesis,
    )
    provider = GoogleProvider(settings.google_api_key, settings.facilitator_model)
    resp = await provider.complete(
        system="You extract durable profile updates from advisory sessions. You are precise and conservative.",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
    )
    return _extract_json(resp.text), resp.cost_usd
