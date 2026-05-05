"""User profile schema.

Profile is stored as JSON in `profile.profile_json` and is what makes Randy feel
personal. It is intentionally small: only durable facts, goals, and decisions.
Per-session detail belongs in the session log, not here.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class UserProfile:
    user_id: str
    goals: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)
    decisions: list[dict[str, str]] = field(default_factory=list)
    things_tried: list[dict[str, str]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    notes: str = ""
    updated_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, user_id: str, raw: str | None) -> "UserProfile":
        if not raw:
            return cls(user_id=user_id)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls(user_id=user_id)
        data["user_id"] = user_id
        # Drop unknown keys to stay forward-compatible.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def is_empty(self) -> bool:
        return not (
            self.goals or self.constraints or self.facts
            or self.decisions or self.things_tried or self.open_questions or self.notes
        )

    def render_markdown(self) -> str:
        if self.is_empty():
            return "_(No profile yet — this is the user's first consultation.)_"
        out: list[str] = []
        if self.goals:
            out.append("**Goals**")
            out.extend(f"- {g}" for g in self.goals)
        if self.constraints:
            out.append("\n**Constraints**")
            out.extend(f"- {c}" for c in self.constraints)
        if self.facts:
            out.append("\n**Facts**")
            out.extend(f"- {k}: {v}" for k, v in self.facts.items())
        if self.decisions:
            out.append("\n**Decisions**")
            out.extend(
                f"- {d.get('date', '?')}: {d.get('what', '')} ({d.get('why', '')})"
                for d in self.decisions
            )
        if self.things_tried:
            out.append("\n**Things tried**")
            out.extend(
                f"- {t.get('date', '?')}: {t.get('what', '')} → {t.get('outcome', '')}"
                for t in self.things_tried
            )
        if self.open_questions:
            out.append("\n**Open questions**")
            out.extend(f"- {q}" for q in self.open_questions)
        if self.notes:
            out.append(f"\n**Notes**\n{self.notes}")
        return "\n".join(out)


def merge_profile_update(current: UserProfile, update: dict[str, Any]) -> UserProfile:
    """Conservative merge: lists union (preserve order), facts overlay, notes overwrite when non-empty.

    The facilitator returns only fields that changed; absent fields keep current values.
    """
    if not update:
        return current

    def _union(existing: list, incoming: list) -> list:
        seen = {json.dumps(x, sort_keys=True) for x in existing}
        merged = list(existing)
        for x in incoming or []:
            key = json.dumps(x, sort_keys=True)
            if key not in seen:
                merged.append(x)
                seen.add(key)
        return merged

    return UserProfile(
        user_id=current.user_id,
        goals=_union(current.goals, update.get("goals", [])),
        constraints=_union(current.constraints, update.get("constraints", [])),
        facts={**current.facts, **(update.get("facts") or {})},
        decisions=_union(current.decisions, update.get("decisions", [])),
        things_tried=_union(current.things_tried, update.get("things_tried", [])),
        open_questions=_union(current.open_questions, update.get("open_questions", [])),
        notes=update.get("notes") or current.notes,
        updated_at=update.get("updated_at", current.updated_at),
    )
