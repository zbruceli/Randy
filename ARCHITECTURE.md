# Randy — Architecture

## High-level shape

```
┌──────────────┐
│   Telegram   │
│ (@RandyBot)  │
└──────┬───────┘
       │ /ask <question>
┌──────▼─────────────────────────────────────────────────┐
│ randy.telegram.bot                                     │
│   gating · slash menu · /profile /recap /cost /r2      │
│   progress pings · md attachments                      │
└──────┬─────────────────────────────────────────────────┘
       │
┌──────▼─────────────────────────────────────────────────┐
│ randy.orchestrator.pipeline.run_consultation           │
│                                                        │
│   1. Load profile (from MemoryStore)                   │
│   2. Build brief (profile + question)                  │
│   3. Round 1: 3 experts in parallel (asyncio.gather)   │
│   4. (opt) Round 2: same experts see others' R1, revise│
│   5. Synthesize (Facilitator: Gemini)                  │
│   6. Persist session + turns                           │
│   7. Fire-and-forget profile updater                   │
└──┬──────────┬──────────┬──────────────┬───────────────┘
   │          │          │              │
┌──▼──┐   ┌──▼──┐    ┌──▼──┐       ┌────▼────┐
│Sonnt│   │GPT5 │    │Deep │       │ Gemini  │
│ R1+ │   │ R1+ │    │Seek │       │  Pro    │
│ R2  │   │ R2  │    │R1+R2│       │ (synth) │
└─────┘   └─────┘    └─────┘       └────┬────┘
                                         │
                                  ┌──────▼──────┐
                                  │ Profile     │
                                  │ updater     │  async,
                                  │ (Gemini Pro)│  after reply
                                  └──────┬──────┘
                                         │
                            ┌────────────▼────────────┐
                            │ randy.memory.MemoryStore│
                            │   SQLite                │
                            │   profile · sessions    │
                            │   turns · user_settings │
                            └─────────────────────────┘
```

Flow per `/ask`:

1. Bot accepts the message, gates by allowlist, pulls user-level R2 setting.
2. Orchestrator loads the profile and constructs the brief that goes to every expert.
3. Round 1: three experts run in parallel through `asyncio.gather`. Each call goes through its own provider adapter, which normalizes I/O and bills the cost meter.
4. If R2 is enabled and ≥2 experts survived R1 and there's cost headroom, a second round runs. Each expert receives the *other* experts' R1 drafts and is asked to (a) critique each by name, (b) revise, (c) name what would change their mind.
5. Facilitator (Gemini Pro) synthesizes the final answer, working from R2 drafts where available else R1.
6. The session and every turn are persisted before the user sees the reply.
7. Profile updater runs async, after the reply has been delivered. Adds zero visible latency.

## Provider layer

`randy.providers` exposes a uniform `Provider.complete(system, messages, **kwargs) -> ProviderResponse` shape across four vendors. Each adapter:

- Calls its native SDK (`anthropic`, `openai`, `google.genai`).
- Returns text + token counts + dollar cost (computed from `pricing.py`).
- Tolerates reasoning-model quirks:
  - Anthropic: walks `resp.content` blocks via `getattr(b, "text", "")`.
  - OpenAI: routes Pro models (`*-pro`) to `/v1/responses`, others to `/v1/chat/completions`. Falls back to `reasoning_content` when `content` is empty (DeepSeek V4 Pro behavior).
  - OpenAI chat path: translates `max_tokens` → `max_completion_tokens` (GPT-5+ requirement).
  - Google: works for both Pro and Flash; handles reasoning-model token consumption with generous `max_tokens`.

DeepSeek's API is OpenAI-compatible, so `DeepSeekProvider` subclasses `OpenAIProvider` with a `base_url` override.

### Cost meter

`CostMeter` is per-session. Every provider call records `(model, cost_usd)`. Two caps:

- `session_cap_usd` — total spend across the session.
- `per_model_cap_usd` — per-model spend within one session.

Hitting either raises `CostCapExceeded`. The orchestrator catches per-call so a R1 expert breach doesn't kill the whole consultation.

## Personas

Four personas live in `src/randy/personas/prompts/*.md` as full system prompts. The `Persona` dataclass and `PERSONAS` registry in `personas/registry.py` load them at import time.

Each persona's prompt contains:

- **Lens** — what kind of thinker they are.
- **How you reason** — the method.
- **Domain bias** — career/startup-specific cues.
- **Voice** — tone constraint.
- **Output format** — required structure.
- **What you are NOT** — explicit anti-overlap with the other personas.
- **Hard rules** — the most common failure modes, blocked.

Anti-convergence design:

- Different vendors per role (different training, different RLHF).
- Different lenses (Strategist=structure, Contrarian=red-team, Operator=execution).
- Round 2 forces each expert to *name disagreements by other expert*, which structurally pushes them apart instead of toward consensus.
- Facilitator's prompt forbids blending three views into mush.

## Round-2 mechanics

When enabled (`/r2 on`):

- Each expert is given a brief that prepends their original prompt with the *other* experts' R1 drafts (not their own).
- The R2 prompt requires three structured outputs: per-expert critique, position revision, and "the one thing that would change my mind."
- R2 drafts replace R1 drafts in the synthesis brief, so the facilitator works with the more refined output. Both rounds are persisted separately in `turns` and shown side-by-side in the `.md` attachment.
- Skipped automatically if <2 experts survived R1, or remaining session headroom < $1.

## Memory model

Two layers, intentional:

### Profile (slow-changing, durable)

`UserProfile` holds:

- `goals` — what the user is aiming for over months/years.
- `constraints` — what they can't or won't do.
- `facts` — role, location, family situation, etc.
- `decisions` — `{date, what, why}` once made.
- `things_tried` — `{date, what, outcome}` for learning loops.
- `open_questions` — unresolved.
- `notes` — free-form.

Stored as JSON in `profile.profile_json`. Updated by the **profile updater** (Gemini, post-reply), which is prompted to be conservative — only persist things that will still matter in 3 months. Returns *only changed fields*; merge semantics:

- Lists: union (preserve order, dedupe by JSON serialization).
- Dicts: overlay.
- Notes: overwrite if non-empty.

### Session log (append-only, fine-grained)

- `sessions` — one row per consultation: topic, started/ended, total cost.
- `turns` — one row per LLM call within a session, tagged with role (`user`, `expert_r1`, `expert_r2`, `facilitator`), persona, model, tokens, cost.

The session log isn't fed back into context for now. It's there for `/recap`, `/cost`, and future analyses.

### Settings

`user_settings` table holds per-user toggles. Currently only `round2_enabled`. Defaults to off.

## Concurrency notes

- All vendor calls are async. R1 is `asyncio.gather` over three experts; R2 same.
- The profile updater runs as a fire-and-forget `asyncio.create_task`. Crashes are logged and don't surface to the user.
- SQLite access is synchronous within `MemoryStore`, called from async handlers. At single-user scale this is fine; if it ever isn't, swap to `aiosqlite` without changing the call sites (they're `def`, not `async def`).

## What's deliberately *not* here

- No vector DB / RAG. Profile + recent context fits in long-context windows easily.
- No clarification round (the original 4-stage design had one). Empirically, the facilitator's synthesis quality didn't justify the extra latency.
- No queueing / sessions registry. One consultation per user at a time, in-process.
- No prompt caching yet. With per-session cost in the $0.05–$0.20 range, the engineering cost of caching exceeds the savings — for now.
