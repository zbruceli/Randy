# Randy — Architecture

## High-level shape

Two delivery channels share one orchestrator stack and one SQLite store. The `ConsultationRunner` sits between the channels and the pure orchestrator pipeline so task tracking, progress streaming, and cancellation are not duplicated.

```
┌──────────────┐                         ┌──────────────────┐
│   Telegram   │                         │  Web (HTMX/JJ)   │
│  (@RandyBot) │                         │ 127.0.0.1:8000   │
└──────┬───────┘                         └────────┬─────────┘
       │ /ask, /new, /r2…                         │ POST /consult, /c/<id>…
┌──────▼─────────────────────┐    ┌───────────────▼───────────────┐
│ randy.telegram.bot         │    │ randy.web.app                  │
│   gating · slash menu      │    │   FastAPI · Jinja · HTMX       │
│   progress pings           │    │   pinned threads · profile UI  │
│   md attachments           │    │   poll /progress/<task>        │
└──────┬─────────────────────┘    └────────┬───────────────────────┘
       │                                   │
       │   push: on_progress callback      │   pull: runner.get_progress
       └────────────┬──────────────────────┘
                    │
┌───────────────────▼──────────────────────────────────────┐
│ randy.orchestrator.runner.ConsultationRunner             │
│   task_id → asyncio.Task + progress lines + result       │
│   start() · subscribe() · get_progress() · wait() · cancel│
└───────────────────┬──────────────────────────────────────┘
                    │ wraps
┌───────────────────▼──────────────────────────────────────┐
│ randy.orchestrator.pipeline.run_consultation             │
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

Flow per consultation (any channel):

1. Channel receives input (Telegram message or web form), gates if needed.
2. Channel calls `runner.start(user_id, question, ...)` → returns a `task_id` immediately. Push channels (Telegram) also register an `on_progress` callback; pull channels (web) poll `runner.get_progress(task_id)`.
3. Inside the runner, `run_consultation` loads the profile (or skips it for `/new`), constructs the brief, and dispatches.
4. Round 1: three experts run in parallel through `asyncio.gather`. Each call goes through its own provider adapter, which normalizes I/O and bills the cost meter.
5. If R2 is enabled and ≥2 experts survived R1 and there's cost headroom, a second round runs. Each expert receives the *other* experts' R1 drafts and is asked to (a) critique each by name, (b) revise, (c) name what would change their mind.
6. Facilitator (Gemini Pro) synthesizes the final answer, working from R2 drafts where available else R1.
7. The session and every turn are persisted before the user sees the reply.
8. Profile updater runs async, after the reply has been delivered. Adds zero visible latency.

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

### Conversations (threads)

A *conversation* is a sustained thread on one topic, made up of multiple sessions linked by `conversation_id`. Created automatically by either channel; auto-titled from the first question (first 60 chars). Sessions in the same conversation see the last 3 syntheses prepended to subsequent briefs by `pipeline._build_thread_context`, keeping token cost predictable as a thread grows.

Pinning is per-user, per-conversation. Archived conversations stay in the DB but are hidden from the home page unless explicitly listed.

Both channels thread:
- **Web**: each `/consult` POST auto-creates a conversation; the conversation page (`/c/<id>`) follow-up form attaches subsequent questions to the same thread.
- **Telegram**: `chat_active_thread(chat_id, conversation_id)` table stores the per-chat active thread across restarts. `/ask` always starts a fresh thread; plain text continues the active one (auto-create if none); `/new` starts a stateless thread (use_profile=False); `/end`, `/threads`, `/here` give explicit control. Post-result inline keyboard offers 📌 Pin and 🗂 End thread.

## Channels

### Telegram (`randy.telegram.bot`)

- Push-style: registers an `on_progress` callback when calling `runner.start(...)`, awaits the task with `runner.wait(task_id)`.
- Cancel button is an inline keyboard with `callback_data="cancel:<chat_id>"`. The bot maps `chat_id → task_id` in `bot_data["chat_tasks"]` and calls `runner.cancel(task_id)`.
- Requires `concurrent_updates(True)` — without it, the cancel callback queues behind the consultation handler.
- Progress edits are debounced (1.2s + asyncio.Lock) to avoid Telegram's ~1/sec/chat edit rate limit deadlocking the orchestrator when R1 experts finish in quick succession.

### Web (`randy.web.app`)

- Pull-style: HTMX polls `/progress/<task_id>` every 2s. Server returns a partial — either the still-running progress card or the final result card.
- Form posts to `/consult` return the placeholder card immediately (HTMX swap). Polling kicks in from there.
- One process per channel — `python -m randy` and `python -m randy.web`. SQLite in WAL mode handles concurrent reads from web and writes from bot.
- Local-only by design (`127.0.0.1:8000`). No auth, no TLS. If you want it remote, add a reverse proxy with auth.

## Concurrency notes

- All vendor calls are async. R1 is `asyncio.gather` over three experts; R2 same.
- The profile updater runs as a fire-and-forget `asyncio.create_task`. Crashes are logged and don't surface to the user.
- SQLite access is synchronous within `MemoryStore`, called from async handlers. At single-user scale this is fine; if it ever isn't, swap to `aiosqlite` without changing the call sites (they're `def`, not `async def`).
- `concurrent_updates(True)` on the Telegram Application — without it, callback-query updates (the cancel button) serialize behind the consultation handler and never fire while the handler is still awaiting its task.
- Progress-edit debounce: when R1's three experts finish in quick succession, each `_run_expert` calls `on_progress("✓ done")` in parallel. Three concurrent `editMessageText` calls on the same chat trigger Telegram's ~1/sec/chat edit rate limit, and PTB's RetryAfter handler then sleeps silently — blocking gather and freezing the orchestrator. The bot wraps `on_progress` in a 1.2s debounce + `asyncio.Lock` so concurrent edits drop instead of queue, and the next eligible edit always shows the latest internal state. `chat_action` keeps firing on every call (it's not rate-limited).

## What's deliberately *not* here

- No vector DB / RAG. Profile + recent context fits in long-context windows easily.
- No clarification round (the original 4-stage design had one). Empirically, the facilitator's synthesis quality didn't justify the extra latency.
- No queueing / sessions registry. One consultation per user at a time, in-process.
- No prompt caching yet. With per-session cost in the $0.05–$0.20 range, the engineering cost of caching exceeds the savings — for now.
