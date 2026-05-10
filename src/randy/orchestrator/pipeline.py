"""End-to-end consultation pipeline.

Phase 4 scope: profile → round 1 (parallel) → round 2 (forced disagreement,
parallel) → facilitator synthesizes. Round 2 is skipped automatically if
fewer than 2 experts survived round 1, or if cost headroom is too thin.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..config import settings
from ..experts.expert import Expert
from ..memory import MemoryStore, UserProfile, merge_profile_update
from ..personas import PERSONAS
from ..providers.anthropic_provider import AnthropicProvider
from ..providers.base import ProviderResponse
from ..providers.cost_meter import CostCapExceeded, CostMeter
from ..providers.deepseek_provider import DeepSeekProvider
from ..providers.google_provider import GoogleProvider
from ..providers.openai_provider import OpenAIProvider
from ..research import ResearchBrief, Researcher
from .profile_updater import extract_profile_update

logger = logging.getLogger("randy.orchestrator")

ProgressFn = Callable[[str], Awaitable[None]]

EXPERT_KEYS = ("strategist", "contrarian", "operator")

# Skip round 2 if doing it would risk blowing the cap. Reserve some headroom for
# the facilitator synthesis at the end.
ROUND2_HEADROOM_USD = 1.0


@dataclass
class ConsultationResult:
    session_id: str
    user_id: str
    question: str
    synthesis: str
    expert_reports: dict[str, str] = field(default_factory=dict)        # final (R2 if available, else R1)
    expert_reports_r1: dict[str, str] = field(default_factory=dict)
    expert_reports_r2: dict[str, str] = field(default_factory=dict)
    expert_costs: dict[str, float] = field(default_factory=dict)        # cumulative per persona
    facilitator_cost: float = 0.0
    research_cost: float = 0.0
    total_cost_usd: float = 0.0
    failures: dict[str, str] = field(default_factory=dict)
    rounds_run: int = 1
    research: ResearchBrief | None = None


def _build_experts() -> dict[str, Expert]:
    p_anthropic = AnthropicProvider(settings.anthropic_api_key, settings.expert_anthropic_model)
    p_openai = OpenAIProvider(
        settings.openai_api_key,
        settings.expert_openai_model,
        api="responses" if "pro" in settings.expert_openai_model.lower() else "chat",
    )
    p_deepseek = DeepSeekProvider(settings.deepseek_api_key, settings.expert_deepseek_model)
    return {
        "strategist": Expert(persona=PERSONAS["strategist"], provider=p_anthropic),
        "contrarian": Expert(persona=PERSONAS["contrarian"], provider=p_openai),
        "operator": Expert(persona=PERSONAS["operator"], provider=p_deepseek),
    }


def _build_facilitator() -> GoogleProvider:
    return GoogleProvider(settings.google_api_key, settings.facilitator_model)


def _build_thread_context(store: "MemoryStore | None", conversation_id: str | None) -> str:
    """If continuing a thread, render the last few sessions' syntheses for context.

    Bounded to last 3 sessions to keep input cost predictable.
    """
    if not store or not conversation_id:
        return ""
    sessions = store.sessions_in_conversation(conversation_id)
    if not sessions:
        return ""
    parts = ["# Prior conversation in this thread"]
    for s in sessions[-3:]:
        turns = store.session_turns(s.session_id)
        synth = next((t["content"] for t in turns if t["role"] == "facilitator"), None)
        parts.append(f"\n## {s.started_at[:10]} — {s.topic}\n")
        if synth:
            parts.append(synth)
    parts.append("\n")
    return "\n".join(parts)


def _build_brief(
    question: str,
    profile: UserProfile,
    thread_context: str = "",
    research_markdown: str = "",
) -> str:
    blocks = [
        "# About this user",
        "",
        profile.render_markdown(),
    ]
    if thread_context:
        blocks.extend(["", thread_context])
    if research_markdown:
        blocks.extend(
            [
                "",
                "# Verified facts (from external research, run before this prompt)",
                "",
                research_markdown,
                "",
                "Treat the above as **reported, attributed data** — not gospel. Cite "
                "the bracketed source whenever you use a fact. Do NOT invent numbers, "
                "dates, or quotes that are not in this section.",
            ]
        )
    blocks.extend(
        [
            "",
            "# The user's question",
            "",
            question,
            "",
            "Use the user-context, prior thread, and verified facts to ground your "
            "answer. Don't repeat them back; use them.",
        ]
    )
    return "\n".join(blocks)


def _format_synthesis_brief(
    question: str,
    profile: UserProfile,
    drafts: dict[str, str],
    thread_context: str = "",
    research_markdown: str = "",
) -> str:
    blocks = []
    for key in EXPERT_KEYS:
        if key in drafts:
            title = PERSONAS[key].title
            blocks.append(f"### {title}\n\n{drafts[key]}")
    expert_section = "\n\n".join(blocks) if blocks else "(no expert drafts available — all experts failed)"

    blocks = [
        "# About this user",
        "",
        profile.render_markdown(),
    ]
    if thread_context:
        blocks.extend(["", thread_context])
    if research_markdown:
        blocks.extend(
            [
                "",
                "# Verified facts (from external research)",
                "",
                research_markdown,
                "",
                "Cite the bracketed source for any fact you use in the synthesis.",
            ]
        )
    blocks.extend(
        [
            "",
            "# User's question",
            "",
            question,
            "",
            "# Expert drafts (post round-2 critique)",
            "",
            expert_section,
            "",
            "# Your task",
            "",
            "Produce the final synthesis for the user, per your persona's output format. "
            "Use the experts' titles by name. The experts have already critiqued each other "
            "in round 2 — surface the live disagreements that remain, don't paper over them. "
            "Keep the response readable on a phone screen.",
        ]
    )
    return "\n".join(blocks)


async def _run_expert(
    key: str,
    expert: Expert,
    brief: str,
    on_progress: ProgressFn | None,
    round_label: str = "",
    prior_drafts: dict[str, str] | None = None,
) -> tuple[str, ProviderResponse | Exception]:
    label = PERSONAS[key].title + (f" ({round_label})" if round_label else "")
    try:
        if on_progress:
            await on_progress(f"  · {label} thinking…")
        resp = await expert.respond(brief, prior_drafts=prior_drafts, max_tokens=3072)
        if on_progress:
            await on_progress(
                f"  ✓ {label} done ({resp.input_tokens}+{resp.output_tokens} tok, ${resp.cost_usd:.3f})"
            )
        return key, resp
    except Exception as e:
        logger.exception("%s expert failed (%s)", key, round_label or "r1")
        if on_progress:
            await on_progress(f"  ✗ {label} failed: {type(e).__name__}")
        return key, e


async def _update_profile_in_background(
    store: MemoryStore,
    user_id: str,
    question: str,
    synthesis: str,
) -> None:
    try:
        profile = store.get_profile(user_id)
        update, _cost = await extract_profile_update(profile, question, synthesis)
        if not update:
            logger.info("no profile update for user=%s", user_id)
            return
        merged = merge_profile_update(profile, update)
        store.save_profile(merged)
        logger.info("profile updated for user=%s keys=%s", user_id, list(update.keys()))
    except Exception:
        logger.exception("profile updater failed (non-fatal)")


async def run_consultation(
    user_id: str,
    question: str,
    store: MemoryStore | None = None,
    on_progress: ProgressFn | None = None,
    round2: bool = False,
    use_profile: bool = True,
    conversation_id: str | None = None,
) -> ConsultationResult:
    session_id = uuid.uuid4().hex[:8]
    meter = CostMeter(
        session_cap_usd=settings.session_cost_cap_usd,
        per_model_cap_usd=settings.per_model_cost_cap_usd,
    )

    if use_profile and store:
        profile = store.get_profile(user_id)
    else:
        # Stateless consultation: no prior context bleeds in, no profile update after.
        profile = UserProfile(user_id=user_id)

    thread_context = _build_thread_context(store, conversation_id) if use_profile else ""

    if store:
        store.start_session(
            session_id, user_id, topic=question[:120], conversation_id=conversation_id
        )
        store.append_turn(session_id, role="user", content=question)

    # Research phase — run BEFORE R1 with a hard timeout. Whatever's gathered
    # gets injected into all expert + facilitator briefs.
    research: ResearchBrief | None = None
    research_markdown = ""
    if on_progress:
        await on_progress("Researching external facts…")
    try:
        researcher = Researcher(store=store)
        research = await researcher.run(
            question=question,
            prior_context=thread_context,
            session_id=session_id if store else None,
            on_progress=on_progress,
        )
        research_markdown = research.markdown if research and research.markdown else ""
        if on_progress and research:
            note = (
                f"  ✓ Research: {research.notes or 'done'}"
                if not research.timed_out
                else f"  ✗ Research timed out after {research.duration_s:.0f}s"
            )
            await on_progress(note)
    except Exception:
        logger.exception("researcher raised; proceeding without research brief")

    experts = _build_experts()
    facilitator = _build_facilitator()
    brief = _build_brief(question, profile, thread_context, research_markdown)

    if on_progress:
        await on_progress("Round 1 — three experts working in parallel…")

    r1_results = await asyncio.gather(
        *(_run_expert(k, e, brief, on_progress, round_label="R1") for k, e in experts.items())
    )

    drafts_r1: dict[str, str] = {}
    expert_costs: dict[str, float] = {}
    failures: dict[str, str] = {}
    for key, outcome in r1_results:
        if isinstance(outcome, Exception):
            failures[key] = f"{type(outcome).__name__}: {outcome}"
            continue
        try:
            meter.record(outcome.model, outcome.cost_usd)
        except CostCapExceeded as e:
            logger.warning("cost cap during round 1: %s", e)
            failures[key] = f"CostCapExceeded: {e}"
            continue
        drafts_r1[key] = outcome.text
        expert_costs[key] = outcome.cost_usd
        if store:
            store.append_turn(
                session_id,
                role="expert_r1",
                persona=key,
                model=outcome.model,
                content=outcome.text,
                tokens_in=outcome.input_tokens,
                tokens_out=outcome.output_tokens,
                cost_usd=outcome.cost_usd,
            )

    # Round 2 — forced disagreement. Caller opts in; we still gate on survivor
    # count and cost headroom as safety nets.
    drafts_r2: dict[str, str] = {}
    rounds_run = 1
    headroom = settings.session_cost_cap_usd - meter.total
    if round2 and len(drafts_r1) >= 2 and headroom > ROUND2_HEADROOM_USD:
        rounds_run = 2
        if on_progress:
            await on_progress("Round 2 — experts critiquing each other and revising…")

        async def _r2_task(key: str) -> tuple[str, ProviderResponse | Exception]:
            others = {k: v for k, v in drafts_r1.items() if k != key}
            return await _run_expert(
                key, experts[key], brief, on_progress, round_label="R2", prior_drafts=others
            )

        r2_results = await asyncio.gather(*(_r2_task(k) for k in drafts_r1))

        for key, outcome in r2_results:
            if isinstance(outcome, Exception):
                failures[f"{key}_r2"] = f"{type(outcome).__name__}: {outcome}"
                continue
            try:
                meter.record(outcome.model, outcome.cost_usd)
            except CostCapExceeded as e:
                logger.warning("cost cap during round 2: %s", e)
                failures[f"{key}_r2"] = f"CostCapExceeded: {e}"
                continue
            drafts_r2[key] = outcome.text
            expert_costs[key] = expert_costs.get(key, 0.0) + outcome.cost_usd
            if store:
                store.append_turn(
                    session_id,
                    role="expert_r2",
                    persona=key,
                    model=outcome.model,
                    content=outcome.text,
                    tokens_in=outcome.input_tokens,
                    tokens_out=outcome.output_tokens,
                    cost_usd=outcome.cost_usd,
                )
    elif not round2:
        logger.info("round 2 disabled by caller")
    elif len(drafts_r1) < 2:
        logger.info("skipping round 2: only %d expert(s) survived round 1", len(drafts_r1))
    else:
        logger.info("skipping round 2: cost headroom $%.2f below threshold", headroom)

    # Synthesis uses R2 where available, falling back to R1.
    final_drafts = {k: drafts_r2.get(k, drafts_r1[k]) for k in drafts_r1}

    if on_progress:
        await on_progress("Synthesizing…")

    synthesis_text = ""
    facilitator_cost = 0.0
    try:
        synth_resp = await facilitator.complete(
            system=PERSONAS["facilitator"].system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": _format_synthesis_brief(
                        question, profile, final_drafts, thread_context, research_markdown
                    ),
                }
            ],
            max_tokens=3072,
        )
        meter.record(synth_resp.model, synth_resp.cost_usd)
        synthesis_text = synth_resp.text
        facilitator_cost = synth_resp.cost_usd
        if store:
            store.append_turn(
                session_id,
                role="facilitator",
                persona="facilitator",
                model=synth_resp.model,
                content=synth_resp.text,
                tokens_in=synth_resp.input_tokens,
                tokens_out=synth_resp.output_tokens,
                cost_usd=synth_resp.cost_usd,
            )
    except Exception as e:
        logger.exception("facilitator failed")
        failures["facilitator"] = f"{type(e).__name__}: {e}"
        if final_drafts:
            synthesis_text = (
                "_(Facilitator failed — raw expert drafts below.)_\n\n"
                + "\n\n".join(
                    f"### {PERSONAS[k].title}\n\n{final_drafts[k]}" for k in EXPERT_KEYS if k in final_drafts
                )
            )
        else:
            synthesis_text = "All experts and the facilitator failed. See logs."

    if store:
        store.end_session(session_id, cost_usd=meter.total)
        if use_profile and synthesis_text and not failures.get("facilitator"):
            asyncio.create_task(
                _update_profile_in_background(store, user_id, question, synthesis_text)
            )

    research_cost = research.cost_usd if research else 0.0
    if research_cost:
        try:
            meter.record(settings.researcher_model, research_cost)
        except CostCapExceeded:
            pass  # already recorded; cost meter just blocks future calls

    return ConsultationResult(
        session_id=session_id,
        user_id=user_id,
        question=question,
        synthesis=synthesis_text,
        expert_reports=final_drafts,
        expert_reports_r1=drafts_r1,
        expert_reports_r2=drafts_r2,
        expert_costs=expert_costs,
        facilitator_cost=facilitator_cost,
        research_cost=research_cost,
        total_cost_usd=meter.total,
        failures=failures,
        rounds_run=rounds_run,
        research=research,
    )
